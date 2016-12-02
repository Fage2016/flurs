from flurs.recommender import feature_recommender

import time
import numpy as np

from logging import getLogger, StreamHandler, Formatter, DEBUG
logger = getLogger(__name__)
handler = StreamHandler()
handler.setFormatter(Formatter('[%(process)d] %(message)s'))
handler.setLevel(DEBUG)
logger.setLevel(DEBUG)
logger.addHandler(handler)


class Evaluator:

    """Base class for experimentation of the incremental models with positive-only feedback.

    """

    def __init__(self, recommender):
        """Set/initialize parameters.

        Args:
            recommender (Recommender): Instance of a recommender.

        """
        self.rec = recommender
        self.is_feature_rec = issubclass(recommender.__class__, feature_recommender.FeatureRecommender)

        # initialize models and user/item information
        self.rec.init_model()

    def set_can_repeat(self, can_repeat):
        self.can_repeat = can_repeat

    def fit(self, train_events, test_events, n_epoch=1):
        """Train a model using the first 30% positive events to avoid cold-start.

        Evaluation of this batch training is done by using the next 20% positive events.
        After the batch SGD training, the models are incrementally updated by using the 20% test events.

        Args:
            train_events (list of Event): Positive training events (0-30%).
            test_events (list of Event): Test events (30-50%).
            n_epoch (int): Number of epochs for the batch training.

        """
        self.rec.init_model()

        # make initial status for batch training
        for e in train_events:
            self.__validate(e)
            self.rec.users[e.user.index]['observed'].add(e.item.index)

        # for batch evaluation, temporarily save new users info
        for e in test_events:
            self.__validate(e)

        self.batch_update(train_events, test_events, n_epoch)

        # batch test events are considered as a new observations;
        # the model is incrementally updated based on them before the incremental evaluation step
        for e in test_events:
            self.rec.users[e.user.index]['observed'].add(e.item.index)
            self.__update(e)

    def batch_update(self, train_events, test_events, n_epoch):
        """Batch update called by the fitting method.

        Args:
            train_events (list of Event): Positive training events (0-20%).
            test_events (list of Event): Test events (20-30%).
            n_epoch (int): Number of epochs for the batch training.

        """
        for epoch in range(n_epoch):
            # SGD requires us to shuffle events in each iteration
            # * if n_epoch == 1
            #   => shuffle is not required because it is a deterministic training (i.e. matrix sketching)
            if n_epoch != 1:
                np.random.shuffle(train_events)

            # 20%: update models
            for e in train_events:
                self.__update(e, is_batch_train=True)

            # 10%: evaluate the current model
            MPR = self.batch_evaluate(test_events)
            logger.debug('epoch %2d: MPR = %f' % (epoch + 1, MPR))

    def batch_evaluate(self, test_events):
        """Evaluate the current model by using the given test events.

        Args:
            test_events (list of Event): Current model is evaluated by these events.

        Returns:
            float: Mean Percentile Rank for the test set.

        """
        percentiles = np.zeros(len(test_events))

        all_items = set(range(self.rec.n_item))
        for i, e in enumerate(test_events):

            # check if the data allows users to interact the same items repeatedly
            unobserved = all_items
            if not self.can_repeat:
                # make recommendation for all unobserved items
                unobserved -= self.rec.users[e.user.index]['observed']
                # true item itself must be in the recommendation candidates
                unobserved.add(e.item.index)

            target_i_indices = np.asarray(list(unobserved))
            recos, scores = self.__recommend(e, target_i_indices)

            pos = np.where(recos == e.item.index)[0][0]
            percentiles[i] = pos / (len(recos) - 1) * 100

        return np.mean(percentiles)

    def evaluate(self, test_events):
        """Iterate recommend/update procedure and compute incremental recall.

        Args:
            test_events (list of Event): Positive test events.

        Returns:
            list of tuples: (rank, recommend time, update time)

        """
        for i, e in enumerate(test_events):
            self.__validate(e)

            # target items (all or unobserved depending on a detaset)
            unobserved = set(range(self.rec.n_item))
            if not self.can_repeat:
                unobserved -= self.rec.users[e.user.index]['observed']
                # * item i interacted by user u must be in the recommendation candidate
                unobserved.add(e.item.index)
            target_i_indices = np.asarray(list(unobserved))

            # make top-{at} recommendation for the 1001 items
            start = time.clock()
            recos, scores = self.__recommend(e, target_i_indices)
            recommend_time = (time.clock() - start)

            rank = np.where(recos == e.item.index)[0][0]

            # Step 2: update the model with the observed event
            self.rec.users[e.user.index]['observed'].add(e.item.index)
            start = time.clock()
            self.__update(e)
            update_time = (time.clock() - start)

            # (top-1 score, where the correct item is ranked, rec time, update time)
            yield scores[0], rank, recommend_time, update_time

    def __update(self, e, is_batch_train=False):
        if self.is_feature_rec:
            self.rec.update_user_feature(e.user.index, e.user.feature)
            self.rec.update_item_feature(e.user.index, e.item.feature)
            self.rec.update(e.user.index, e.item.index, e.value, e.context,
                            is_batch_train=is_batch_train)
        else:
            self.rec.update(e.user.index, e.item.index, e.value,
                            is_batch_train=is_batch_train)

    def __recommend(self, e, target_i_indices):
        if self.is_feature_rec:
            self.rec.update_user_feature(e.user.index, e.user.feature)
            self.rec.update_item_feature(e.item.index, e.item.feature)
            return self.rec.recommend(e.user.index, target_i_indices, e.context)
        else:
            return self.rec.recommend(e.user.index, target_i_indices)

    def __validate(self, e):
        self.__validate_user(e)
        self.__validate_item(e)

    def __validate_user(self, e):
        if not self.rec.is_new_user(e.user.index):
            return

        if self.is_feature_rec:
            self.rec.add_user(e.user.index, e.context)
        else:
            self.rec.add_user(e.user.index)

    def __validate_item(self, e):
        if not self.rec.is_new_item(e.item.index):
            return

        if self.is_feature_rec:
            self.rec.add_item(e.item.index, e.item.feature)
        else:
            self.rec.add_item(e.item.index)
