#! /usr/bin/env python3

#############################################################################
##
## This file is (almost) a copy of dense_single_layer, with modifications to
## network to support dropout. Ideally, you should be able to diff the two
## and propogate changes as appropriate
##
## Okay. So that was a nice dream. That turned into a lie. But it started as
## a copy! Hopefully you can figure out what's going on.
##
#############################################################################

import tensorflow as tf
layers = tf.contrib.layers
import numpy as np
from sklearn import datasets
import os
import sys

try:
    import colored_traceback.always
except ImportError:
    pass

from plaid_data_setup import get_input_len, get_labels_len, train_cycle_nn, gen_data

import DeepIoT_compressor
import DeepIoT_dropOut
import DeepIoT_utilities

# Config:
n_hidden         = 30*11
n_input          = get_input_len()
n_labels         = get_labels_len()
learning_rate    = 0.001
BATCH_SIZE       = 50

# Initial probabilities for drop rates
FC_KEEP_PROBABILITY = 0.5

# Initialize vectors for each layer with the initial dropout probabilty for each
# node
#
# XXX Network-specific
dropout_probabilities_fc1 = tf.get_variable(
        "dropout_probabilities_fc1",
        shape=[1, n_hidden],
        dtype=tf.float64,
        initializer=tf.constant_initializer(FC_KEEP_PROBABILITY),
        trainable=False,
        )

# prun_thres
#
# This single scalar is learned and is the threshold to prune a node from the
# network. It is a global that is updated as the compressor phase runs.
prune_threshold = tf.get_variable("prune_threshold",
        shape=[],
        dtype=tf.float64,
        initializer=tf.constant_initializer(0.0),
        trainable=False,
        )

# was "sol_train"
#
# Indicator used in dropout creation that changes dropout behavior based on
# whether we are in the compress or fine-tune phase
compress_done = tf.Variable(0,
        dtype=tf.float64,
        trainable=False,
        )

# prob_list_dict
layer_name_to_probability_variables = {
        'fc1': dropout_probabilities_fc1,
        }

# org_dim_dict
layer_name_to_original_dimensions = {
        'fc1': n_hidden,
        }

# A variable that I highly suspect was used for debugging and doesn't actually
# do anything:
compressor_global_step = tf.Variable(0, trainable=False)

def build_nn(BatchedTrainingData, BatchedTrainingLabels, BatchedEvalData, BatchedEvalLabels):
    #graph = tf.Graph()
    #with graph.as_default():

    # neural network inputs and expected results
    # X = tf.placeholder("float", [None, n_input])
    # Y = tf.placeholder("float", [None, n_labels])
    # Y_eval = tf.placeholder("float", [None, n_labels])
    # dropout_prob = tf.placeholder_with_default(1.0, shape=())

    # # neural network parameters
    # weights = {
    #     'h1':  tf.Variable(tf.random_normal([n_input, n_hidden])),
    #     'out': tf.Variable(tf.random_normal([n_hidden, n_labels])),
    # }
    # biases = {
    #     'b1':   tf.Variable(tf.random_normal([n_hidden])),
    #     'out':  tf.Variable(tf.random_normal([n_labels])),
    # }


    def neural_net(inputs, is_training, batch_size, reuse=False, name='dense_single'):
        with tf.variable_scope(name, reuse=reuse) as scope:
            # layer_name: drop mask
            out_binary_mask = {}

            # # hidden fully connected layer
            # layer_1 = tf.nn.tanh(tf.add(tf.matmul(x, weights['h1']), biases['b1']))
            fc1 = layers.fully_connected(inputs,
                                         n_hidden, # num_outputs
                                         activation_fn = tf.nn.tanh,
                                         scope='fc1',
                                         )

            # # dropout on hidden layer
            # layer_1_drop = tf.nn.dropout(layer_1, dropout_prob)

            # Fancy dropout, dropped_layer behavior depends on `is_training`
            # input, keep prob, is_training, noise_shape, seed, name -> (dropped_layer, whether_dropped)
            fc1_post_drop, fc1_drop_mask = DeepIoT_dropOut.dropout(fc1,
                    DeepIoT_utilities.dropout_prune(
                        dropout_probabilities_fc1, prune_threshold, compress_done),
                    is_training=is_training,
                    noise_shape=(1,330),
                    # noise_shape, seed??
                    name='fc1_dropout',
                    )

            out_binary_mask[u'fc1'] = fc1_drop_mask

            # # output fully connected layer, neuron for each class
            # out_layer = tf.matmul(layer_1_drop, weights['out']) + biases['out']
            output = layers.fully_connected(fc1_post_drop,
                                         n_labels, # num_outputs
                                         activation_fn = None,
                                         scope='output',
                                         )

            return output, out_binary_mask


    # Construct *two copies* of the network, the first is the 'critic',
    # where the network is actively training and the second (the _eval
    # family) is a copy that is periodically evaluted (_without_ training)
    # on the evaluation data.

    # Construct Critic ("discOptimizer")
    logits, out_binary_mask = neural_net(BatchedTrainingData, True, BATCH_SIZE, name="dense_single")
    prediction_train = tf.nn.softmax(logits) # reduce unscaled values to probabilities

    # May be unneeded or a duplicate of the `with graph` above
    nn_variables_to_optimize = [var for var in tf.trainable_variables() if 'dense_single/' in var.name]

    # Define loss and optimizer
    batch_loss_train = tf.nn.softmax_cross_entropy_with_logits(logits=logits, labels=BatchedTrainingLabels)
    loss_train = tf.reduce_mean(batch_loss_train)
    optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
    train_op = optimizer.minimize(loss_train, var_list=nn_variables_to_optimize)
    discOptimizer = train_op # alais to DeepIoT name for sanity

    # Compute model accuracy
    predictions = tf.argmax(prediction_train, 1)
    pred_scores = tf.reduce_max(prediction_train,1)
    pred_scores_full = prediction_train
    correct_pred = tf.equal(predictions, tf.argmax(BatchedTrainingLabels, 1)) # check the index with the largest value
    accuracy_train = tf.reduce_mean(tf.cast(correct_pred, tf.float64)) # percentage of traces that were correct

    train_TF_ops = (discOptimizer, loss_train, BatchedTrainingLabels, prediction_train, accuracy_train)

    # Construct Evaluation network
    #
    # n.b. I _think_ that the 'reuse=True' and the variable name scoping
    # means that this isn't actually a copy, rather it references most of
    # the same TF variables, but with the 'is_training' flag set to false in
    # this TF execution unit.
    logits_eval, out_binary_mask_eval = neural_net(BatchedEvalData, False, BATCH_SIZE, reuse=True, name="dense_single")
    prediction_eval = tf.nn.softmax(logits_eval)
    loss_eval = tf.reduce_mean(
            tf.nn.softmax_cross_entropy_with_logits(
                logits=logits_eval, labels=BatchedEvalLabels))

    eval_TF_ops = (loss_eval, BatchedEvalLabels, prediction_eval)


    #####################
    # Create `Compressor`

    # Need some extra stats from the loss of the batch running in the critic:
    # XXX consider tf.nn.batch_normalization
    batch_loss_mean, batch_loss_variance = tf.nn.moments(batch_loss_train, axes = [0])
    loss_mean = tf.reduce_mean(batch_loss_mean)
    loss_std = tf.reduce_mean(tf.sqrt(batch_loss_variance))

    # Have TF track the ema of loss to smooth the impact of any one run. Also
    # used in the compressor_loss function
    ema = tf.train.ExponentialMovingAverage(0.9)
    maintain_averages_op = ema.apply([loss_mean, loss_std])

    # It is a scary bit of magic to me that these are the same variables as
    # above, but here we go
    drop_prob_dict = DeepIoT_compressor.compressor(nn_variables_to_optimize, inter_dim=n_hidden)

    compressor_loss = DeepIoT_compressor.gen_compressor_loss(
            drop_prob_dict, out_binary_mask,
            BATCH_SIZE, batch_loss_train, ema, loss_mean, loss_std,
            prune_threshold, compress_done)
    update_drop_op__dict_of_TF_things_to_run = DeepIoT_compressor.update_drop_op(
            drop_prob_dict, layer_name_to_probability_variables)

    # `compressor/` namespace defined in DeepIoT_compressor.compressor(..)
    compressor_variables_to_optimize =\
            [var for var in tf.trainable_variables() if 'compressor/' in var.name]
    compressor_optimizer = tf.train.RMSPropOptimizer(0.001).minimize(compressor_loss,
            var_list=compressor_variables_to_optimize, global_step=compressor_global_step)

    # "layer_name: <tf.fn>[compute: count_of_nodes_not_pruned]" (those "left")
    left_num_dict = DeepIoT_utilities.count_prun(
            layer_name_to_probability_variables, prune_threshold)

    train_TF_ops = (discOptimizer, loss_train, BatchedTrainingLabels, prediction_train, accuracy_train)
    compressor_TF_ops = (compressor_optimizer, compressor_loss,
            maintain_averages_op, ema.average(loss_mean), ema.average(loss_std))

    return train_TF_ops, eval_TF_ops, compressor_TF_ops

if __name__ == "__main__":
    # train the neural network on test data
    #graph, X, Y, optimizer, dropout_prob, evaluation_args = build_nn()
    #train_cycle_nn(graph, X, Y, optimizer, dropout_prob, evaluation_args, gen_data())

    # Get data
    print("Loading data...")
    TrainingData, ValidationData, TrainingLabels, ValidationLabels,\
            TrainingNames, ValidationNames, labelstrs, num_names = gen_data()

    # convert Labels from integers to one-hot array
    n_classes = len(labelstrs)
    OneHotTrainingLabels = np.eye(n_classes)[TrainingLabels.astype(np.int64)]
    OneHotValidationLabels = np.eye(n_classes)[ValidationLabels.astype(np.int64)]

    ### # Create Batches
    ### print("Creating batches...")
    ### # Minimum number elements in the queue after a dequeue, used to ensure a level of mixing of elements.
    ### min_after_dequeue = 1000
    ### # capacity: An integer. The maximum number of elements in the queue.
    ### capacity = min_after_dequeue + 3 * BATCH_SIZE
    ### batch_training_features, batch_training_labels = tf.train.shuffle_batch(
    ###         [TrainingData, OneHotTrainingLabels], batch_size=BATCH_SIZE,
    ###         num_threads=16, capacity=capacity, min_after_dequeue=min_after_dequeue)
    ### batch_eval_features, batch_eval_labels = tf.train.shuffle_batch(
    ###         [ValidationData, OneHotValidationLabels], batch_size=BATCH_SIZE,
    ###         num_threads=16, capacity=capacity, min_after_dequeue=min_after_dequeue)

    # Create Batches
    print("Creating batches...")
    batch_training_features = tf.placeholder("float64", [None, n_input])
    batch_training_labels = tf.placeholder("float64", [None, n_labels])
    batch_eval_features = tf.placeholder("float64", [None, n_input])
    batch_eval_labels = tf.placeholder("float64", [None, n_labels])

    # Create NN's
    print("Creating NN's...")
    training_NN, eval_NN, compressor_NN = build_nn(
            BatchedTrainingData=batch_training_features, BatchedTrainingLabels=batch_training_labels,
            BatchedEvalData=batch_eval_features, BatchedEvalLabels=batch_eval_labels,
            )

    print("Creating TF session...")
    with tf.Session() as sess:
        tf.global_variables_initializer().run()
        saver = tf.train.Saver()
        coord = tf.train.Coordinator()

        CACHE_PATH = 'DeepIoT-cache'
        if os.path.exists(CACHE_PATH):
            print("Loading pre-trained, uncompressed model")
            saver.restore(sess, os.path.join(CACHE_PATH, 'model'))
            print("Loaded\n")

        else:
            print('='*80)
            print("Training initial model (no dropout)")
            for iteration in range(50000):
                # select data to train on and test on for this iteration
                batch_nums = np.random.choice(TrainingData.shape[0], BATCH_SIZE)

                # Run training
                optimizer, loss, labels, prediction, accuracy = sess.run(training_NN,
                        feed_dict = {
                            batch_training_features: TrainingData[batch_nums],
                            batch_training_labels: OneHotTrainingLabels[batch_nums],
                            }
                        )

                if (iteration < 5) or (iteration % 999) == 0:
                    # select data to evaluate for this iteration
                    batch_nums_eval = np.random.choice(ValidationData.shape[0], BATCH_SIZE)

                    # Run evaluation
                    loss_eval, labels_eval, prediction_eval = sess.run(eval_NN,
                            feed_dict = {
                                batch_eval_features: ValidationData[batch_nums_eval],
                                batch_eval_labels: OneHotValidationLabels[batch_nums_eval],
                                }
                            )

                    print("iteration {:06}".format(iteration), end='')
                    print(" | training loss {} accuracy {}".format(loss, accuracy), end='')
                    print(" | evalaution loss {}".format(loss_eval))
            print("Finished initial training.")
            print('='*80)

            saver.save(sess, os.path.join(CACHE_PATH, 'model'))

        print("Running loaded model on evaluation data once first:")
        # Run evaluation
        batch_nums_eval = np.random.choice(ValidationData.shape[0], BATCH_SIZE)
        loss_eval, labels_eval, prediction_eval = sess.run(eval_NN,
                feed_dict = {
                    batch_eval_features: ValidationData[batch_nums_eval],
                    batch_eval_labels: OneHotValidationLabels[batch_nums_eval],
                    }
                )

        print(" | evalaution loss {}".format(loss_eval))
