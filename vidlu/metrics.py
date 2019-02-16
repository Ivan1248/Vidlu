from functools import partial

import numpy as np
from sklearn.metrics import confusion_matrix
import torch
from ignite import metrics

from vidlu.problem import Problem, dataset_to_problem

EPS = 1e-8


# from dlu #########################################################################################


class AccumulatingMetric:
    def reset(self):
        raise NotImplementedError()

    def update(self, iter_output):
        raise NotImplementedError()

    def compute(self):
        raise NotImplementedError()


class FuncMetric(AccumulatingMetric):
    def __init__(self, func, name=None):
        self.func = func
        self.name = name or type(self).__name__
        self.reset()

    def reset(self):
        self._sum = 0
        self._n = EPS

    def update(self, iter_output):
        self._sum += self.func(iter_output)
        self._n += 1

    def compute(self):
        return {self.name: self._sum / self._n}


class NumPyClassificationMetrics(AccumulatingMetric):
    def __init__(self, class_count):
        self.class_count = class_count
        self.cm = np.zeros([class_count] * 2)
        self.labels = np.arange(class_count)
        self.active = False

    def reset(self):
        self.cm.fill(0)

    def update(self, iter_output):
        self.cm += confusion_matrix(iter_output.target.flatten(),
                                    iter_output.hard_prediction.flatten(),
                                    labels=self.labels)

    def compute(self, returns=('A', 'mP', 'mR', 'mF1', 'mIoU')):
        # Computes macro-averaged classification evaluation metrics based on the
        # accumulated confusion matrix and clears the confusion matrix.
        tp = np.diag(self.cm)
        actual_pos = self.cm.sum(axis=1)
        pos = self.cm.sum(axis=0)
        fp = pos - tp
        with np.errstate(divide='ignore', invalid='ignore'):
            P = tp / pos
            R = tp / actual_pos
            F1 = 2 * P * R / (P + R)
            IoU = tp / (actual_pos + fp)
            P, R, F1, IoU = map(np.nan_to_num, [P, R, F1, IoU])  # 0 where tp=0
        mP, mR, mF1, mIoU = map(np.mean, [P, R, F1, IoU])
        A = tp.sum() / pos.sum()
        locs = locals()
        return dict((x, locs[x]) for x in returns)


class ClassificationMetrics(AccumulatingMetric):
    def __init__(self, class_count):
        self.class_count = class_count
        self.cm = np.zeros([class_count] * 2)
        self.labels = np.arange(class_count)
        self.active = False

    def reset(self):
        self.cm.fill(0)

    def update(self, iter_output):
        pred = iter_output.target.flatten().cpu().numpy()
        true = iter_output.outputs.hard_prediction.flatten().cpu().numpy()
        self.cm += confusion_matrix(pred, true, labels=self.labels)

    def compute(self, returns=('A', 'mP', 'mR', 'mF1', 'mIoU'), eps=1e-8):
        # Computes macro-averaged classification evaluation metrics based on the
        # accumulated confusion matrix and clears the confusion matrix.
        tp = np.diag(self.cm)
        actual_pos = self.cm.sum(axis=1)
        pos = self.cm.sum(axis=0) + eps
        fp = pos - tp

        P = tp / pos
        R = tp / actual_pos
        F1 = 2 * P * R / (P + R)
        IoU = tp / (actual_pos + fp)
        P, R, F1, IoU = map(np.nan_to_num, [P, R, F1, IoU])  # 0 where tp=0

        mP, mR, mF1, mIoU = map(np.mean, [P, R, F1, IoU])
        A = tp.sum() / pos.sum()
        locs = locals()
        return dict((x, locs[x]) for x in returns)


# get_default_metrics ##############################################################################

def get_default_metric_args(dataset):
    problem = dataset_to_problem(dataset)
    if problem == Problem.CLASSIFICATION:
        return dict(class_count=dataset.info.class_count)
    elif problem == Problem.SEMANTIC_SEGMENTATION:
        return dict(class_count=dataset.info.class_count)
    elif problem == Problem.DEPTH_REGRESSION:
        return dict()
    elif problem == Problem.OTHER:
        return dict()


def get_default_metrics(dataset):
    problem = dataset_to_problem(dataset)
    if problem in [Problem.CLASSIFICATION, Problem.SEMANTIC_SEGMENTATION]:
        return [partial(FuncMetric, func=lambda io: io.loss, name='loss'),
                partial(ClassificationMetrics, class_count=dataset.info.class_count)]
    elif problem == Problem.DEPTH_REGRESSION:
        return [metrics.MeanSquaredError, metrics.MeanAbsoluteError]
    elif problem == Problem.OTHER:
        return []
