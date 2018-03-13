import datetime
import json
import os
import pickle as pk
import random

import keras
import keras.regularizers as regularizers
import numpy as np
from keras.layers import Input, Dense
from keras.models import Model
from keras.optimizers import Adam
from scipy.stats import mode
from sklearn.externals import joblib
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import SGDClassifier
import pescador

from classifier.metrics import compute_metrics, collapse_metrics
from data.usc.us8k import get_us8k_batch_generator, get_us8k_batch, load_test_fold
from l3embedding.train import LossHistory
from log import *

LOGGER = logging.getLogger('classifier')
LOGGER.setLevel(logging.DEBUG)


class MetricCallback(keras.callbacks.Callback):

    def __init__(self, valid_data, verbose=False):
        super(MetricCallback).__init__()
        self.valid_data = valid_data
        self.verbose = verbose

    def on_train_begin(self, logs=None):
        if logs is None:
            logs = {}
        self.train_loss = []
        self.valid_loss = []
        self.train_acc = []
        self.valid_acc = []
        self.valid_class_acc = []
        self.valid_avg_class_acc = []

    # def on_batch_end(self, batch, logs={}):
    def on_epoch_end(self, epoch, logs=None):
        if logs is None:
            logs = {}
        self.train_loss.append(logs.get('loss'))
        self.valid_loss.append(logs.get('val_loss'))
        self.train_acc.append(logs.get('acc'))
        self.valid_acc.append(logs.get('val_acc'))

        valid_pred = self.model.predict(self.valid_data['features'])
        valid_metrics = compute_metrics(self.valid_data['label'], valid_pred)
        self.valid_class_acc.append(valid_metrics['class_accuracy'])
        self.valid_avg_class_acc.append(valid_metrics['average_class_accuracy'])

        if self.verbose:
            train_msg = 'Train - loss: {}, acc: {}'
            valid_msg = 'Valid - loss: {}, acc: {}'
            LOGGER.info('Epoch {}'.format(epoch))
            LOGGER.info(train_msg.format(self.train_loss[-1],
                                         self.train_acc[-1]))
            LOGGER.info(valid_msg.format(self.valid_loss[-1],
                                         self.valid_acc[-1]))


def train_svm(train_gen, valid_data, test_data, model_dir, C=1e-4, reg_penalty='l2',
              num_workers=1, tol=1e-3, max_iterations=1000000, verbose=False, **kwargs):
    """
    Train a Support Vector Machine model on the given data

    Args:
        X_train: Training feature data
                 (Type: np.ndarray)
        y_train: Training label data
                 (Type: np.ndarray)
        X_test: Testing feature data
                (Type: np.ndarray)
        y_test: Testing label data
                (Type: np.ndarray)

    Keyword Args:
        C: SVM regularization hyperparameter
           (Type: float)

        verbose:  If True, print verbose messages
                  (Type: bool)

    Returns:
        clf: Classifier object
             (Type: sklearn.svm.SVC)

        y_train_pred: Predicted train output of classifier
                     (Type: np.ndarray)

        y_test_pred: Predicted test output of classifier
                     (Type: np.ndarray)
    """
    # Set up standardizer
    stdizer = StandardScaler()

    X_valid = valid_data['features']
    y_valid = valid_data['labels']

    train_loss_history = []
    valid_loss_history = []
    train_metric_history = []
    valid_metric_history = []

    model_output_path = os.path.join(model_dir, "model.pkl")

    # Create classifier
    clf = SGDClassifier(alpha=C, penalty=reg_penalty, n_jobs=num_workers, verbose=verbose)

    LOGGER.debug('Fitting model to data...')
    for iter_idx, train_data in enumerate(train_gen):
        X_train = train_data['features']
        y_train = train_data['labels']
        stdizer.partial_fit(X_train)

        # Fit data and get output for train and valid batches
        clf.partial_fit(X_train, y_train)
        y_train_pred = clf.predict(stdizer.transform(X_train))
        y_valid_pred = clf.predict(stdizer.transform(X_valid))

        # Compute new metrics
        valid_loss_history.append(list(y_valid_pred))
        train_loss_history.append(clf.loss_function_(y_train, y_train_pred))
        valid_loss_history.append(clf.loss_function_(y_valid, y_valid_pred))
        train_metric_history.append(compute_metrics(y_train, y_train_pred))
        valid_metric_history.append(compute_metrics(y_valid, y_valid_pred))

        # Save the model for this iteration
        LOGGER.info('Saving model...')
        joblib.dump(clf, model_output_path)

        if verbose:
            train_msg = 'Train - loss: {}, acc: {}'
            valid_msg = 'Valid - loss: {}, acc: {}'
            LOGGER.info('Epoch {}'.format(iter_idx + 1))
            LOGGER.info(train_msg.format(train_loss_history[-1],
                                         train_metric_history[-1]['accuracy']))
            LOGGER.info(valid_msg.format(valid_loss_history[-1],
                                         valid_metric_history[-1]['accuracy']))

        # Finish training if the loss doesn't change much
        if len(train_loss_history) > 1 and abs(train_loss_history[-2] - train_loss_history[-1]) < tol:
            break

        # Break if we reach the maximum number of iterations
        if iter_idx >= max_iterations:
            break

    # Post process metrics
    train_metrics = collapse_metrics(train_metric_history)
    valid_metrics = collapse_metrics(valid_metric_history)
    train_metrics['loss'] = train_loss_history
    valid_metrics['loss'] = valid_loss_history

    # Evaluate model on test data
    X_test = stdizer.transform(test_data['features'])
    y_test_pred_frame = clf.predict(X_test)
    y_test_pred = []
    for start_idx, end_idx in test_data['file_idxs']:
        class_pred = mode(y_test_pred_frame[start_idx:end_idx])[0][0]
        y_test_pred.append(class_pred)

    y_test_pred = np.array(y_test_pred)
    test_metrics = compute_metrics(test_data['labels'], y_test_pred)

    return clf, train_metrics, valid_metrics, test_metrics


def construct_mlp_model(input_shape, weight_decay=1e-5):
    """
    Constructs a multi-layer perceptron model

    Args:
        input_shape: Shape of input data
                     (Type: tuple[int])
        weight_decay: L2 regularization factor
                      (Type: float)

    Returns:
        model: L3 CNN model
               (Type: keras.models.Model)
        input: Model input
               (Type: list[keras.layers.Input])
        output:Model output
                (Type: keras.layers.Layer)
    """
    l2_weight_decay = regularizers.l2(weight_decay)
    inp = Input(shape=input_shape, dtype='float32')
    y = Dense(512, activation='relu', kernel_regularizer=l2_weight_decay)(inp)
    y = Dense(128, activation='relu', kernel_regularizer=l2_weight_decay)(y)
    y = Dense(10, activation='softmax', kernel_regularizer=l2_weight_decay)(y)
    m = Model(inputs=inp, outputs=y)
    m.name = 'urban_sound_classifier'

    return m, inp, y


def train_mlp(train_gen, valid_data, test_data, model_dir,
              batch_size=64, num_epochs=100, train_epoch_size=None,
              learning_rate=1e-4, weight_decay=1e-5,
              verbose=False, **kwargs):
    """
    Train a Multi-layer perceptron model on the given data

    Args:
        X_train: Training feature data
                 (Type: np.ndarray)
        y_train: Training label data
                 (Type: np.ndarray)
        X_test: Testing feature data
                (Type: np.ndarray)
        y_test: Testing label data
                (Type: np.ndarray)
        model_dir: Path to model directory
                   (Type: str)
        frame_features: If True, test data will be handled as a tensor, where
                        the first dimension is the audio file, and the second
                        dimension is the frame in the audio file. Evaluation
                        will additionally be done at the audio file level
                        (Type: bool)

    Keyword Args:
        verbose:  If True, print verbose messages
                  (Type: bool)
        batch_size: Number of smamples per batch
                    (Type: int)
        num_epochs: Number of training epochs
                    (Type: int)
        train_epoch_size: Number of training batches per training epoch
                          (Type: int)
        learning_rate: Learning rate value
                       (Type: float)
        weight_decay: L2 regularization factor
                      (Type: float)
    """
    loss = 'categorical_crossentropy'
    metrics = ['accuracy']
    monitor = 'val_loss'

    # Set up data inputs
    train_gen = pescador.maps.keras_tuples(train_gen, 'features', 'label')
    valid_data_keras = (valid_data['features'], valid_data['label'])
    train_iter = iter(train_gen)
    train_batch = next(train_iter)

    # Set up model
    m, inp, out = construct_mlp_model(train_batch['features'].shape[1:], weight_decay=weight_decay)

    # Set up callbacks
    cb = []
    weight_path = os.path.join(model_dir, 'model.h5')
    cb.append(keras.callbacks.ModelCheckpoint(weight_path,
                                              save_weights_only=True,
                                              save_best_only=True,
                                              monitor=monitor))
    history_checkpoint = os.path.join(model_dir, 'history_checkpoint.pkl')
    cb.append(LossHistory(history_checkpoint))
    history_csvlog = os.path.join(model_dir, 'history_csvlog.csv')
    cb.append(keras.callbacks.CSVLogger(history_csvlog, append=True,
                                        separator=','))
    metric_cb = MetricCallback(valid_data, verbose=verbose)
    cb.append(metric_cb)

    # Fit model
    LOGGER.debug('Compiling model...')
    m.compile(Adam(lr=learning_rate), loss=loss, metrics=metrics)
    LOGGER.debug('Fitting model to data...')
    m.fit_generator(train_gen, batch_size=batch_size,
          epochs=num_epochs, steps_per_epoch=train_epoch_size,
          validation_data=valid_data_keras, callbacks=cb)

    # Set up train and validation metrics
    train_metrics = {
        'loss': metric_cb.train_loss,
        'acc': metric_cb.train_acc
    }

    valid_metrics = {
        'loss': metric_cb.valid_loss,
        'acc': metric_cb.valid_acc,
        'class_acc': metric_cb.valid_class_acc,
        'avg_class_acc': metric_cb.valid_avg_class_acc
    }

    # Evaluate model on test data
    X_test = test_data['features']
    y_test_pred_frame = m.predict(X_test)
    y_test_pred = []
    for start_idx, end_idx in test_data['file_idxs']:
        class_pred = mode(y_test_pred_frame[start_idx:end_idx])[0][0]
        y_test_pred.append(class_pred)
    y_test_pred = np.array(y_test_pred)
    test_metrics = compute_metrics(test_data['labels'], y_test_pred)

    return m, train_metrics, valid_metrics, test_metrics


def train(features_dir, output_dir, model_id, fold_num, model_type='svm',
          num_streamers=None, batch_size=64, mux_rate=None, random_state=20171021,
          verbose=False, **model_args):
    init_console_logger(LOGGER, verbose=verbose)
    LOGGER.debug('Initialized logging.')

    # Set random state
    np.random.seed(random_state)
    random.seed(random_state)

    # Make sure the directories we need exist
    model_dir = os.path.join(output_dir, model_id,
                             'fold{}'.format(fold_num),
                             datetime.datetime.now().strftime("%Y%m%d%H%M%S"))

    # Make sure model directory exists
    if not os.path.isdir(model_dir):
        os.makedirs(model_dir)

    # Save configs
    with open(os.path.join(model_dir, 'config.json'), 'w') as fp:
        config = {
            'features_dir': features_dir,
            'output_dir': output_dir,
            'model_id': model_id,
            'fold_num': fold_num,
            'model_type': model_type,
            'num_streamers': num_streamers,
            'batch_size': batch_size,
            'mux_rate': mux_rate,
            'random_state': random_state,
            'verbose': verbose
        }
        config.update(model_args)

        json.dump(config, fp)

    LOGGER.info('Loading data...')

    fold_idx = fold_num - 1
    LOGGER.info('Preparing data for fold {}'.format(fold_num))
    train_gen = get_us8k_batch_generator(features_dir, fold_idx,
                         valid=False, num_streamers=num_streamers,
                         batch_size=batch_size, random_state=random_state,
                         rate=mux_rate)
    valid_data = get_us8k_batch(features_dir, fold_idx,
                         valid=True, num_streamers=num_streamers,
                         batch_size=batch_size, random_state=random_state,
                         rate=mux_rate)
    test_data = load_test_fold(features_dir, fold_idx)

    LOGGER.info('Training {} with fold {} held out'.format(model_type, fold_num))
    # Fit the model
    if model_type == 'svm':
        model, train_metrics, valid_metrics, test_metrics \
            = train_svm(train_gen, valid_data, test_data, model_dir,
                verbose=verbose, **model_args)

    elif model_type == 'mlp':
        model, train_metrics, valid_metrics, test_metrics \
                = train_mlp(train_gen, valid_data, test_data, model_dir,
                batch_size=batch_size, verbose=verbose, **model_args)

    else:
        raise ValueError('Invalid model type: {}'.format(model_type))

    # Assemble metrics for this training run
    results = {
        'train': train_metrics,
        'valid': valid_metrics,
        'test': test_metrics
    }

    LOGGER.info('Done training. Saving results to disk...')

    # Save results to disk
    results_file = os.path.join(model_dir, 'results.pkl')
    with open(results_file, 'w') as fp:
        pk.dump(results, fp, protocol=pk.HIGHEST_PROTOCOL)

    LOGGER.info('Done!')
