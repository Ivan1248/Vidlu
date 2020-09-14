import inspect
import warnings
from functools import partial

from vidlu.models import DiscriminativeModel, Autoencoder, MNISTNet
from vidlu.factories.problem import (Classification, SemanticSegmentation, DepthRegression,
                                     get_problem_type)
import vidlu.training as t
from vidlu.utils.func import ArgTree, params


# Problem ######################################################################

def get_problem_from_dataset(dataset):
    if 'problem' not in dataset.info:
        raise ValueError("Unknown problem.")
    problem_type = get_problem_type(dataset.info.problem)
    args = {k: tuple(dataset[0].y.shape) if k == "y_shape" else dataset.info[k]
            for k in params(problem_type)}
    return problem_type(**args)


# Model ############################################################################################

def get_model_argtree_for_problem(model_class, problem):
    from vidlu.modules import components

    if inspect.isclass(model_class):
        if issubclass(model_class, DiscriminativeModel):
            if type(problem) is Classification:
                if model_class in [MNISTNet]:
                    return ArgTree(head_f=partial(components.ClassificationHead1D,
                                                  class_count=problem.class_count))
                return ArgTree(head_f=partial(components.ClassificationHead,
                                              class_count=problem.class_count))
            elif type(problem) is SemanticSegmentation:
                if not isinstance(params(model_class).head_f, components.SegmentationHead):
                    return ArgTree(head_f=partial(components.SegmentationHead,
                                                  class_count=problem.class_count,
                                                  shape=problem.y_shape))
            elif type(problem) is DepthRegression:
                return ArgTree(head_f=partial(components.RegressionHead, shape=problem.y_shape))
            else:
                raise ValueError("Invalid problem type.")
        elif issubclass(model_class, Autoencoder):
            return ArgTree()
    elif model_class.__module__.startswith('torchvision.models'):
        if isinstance(problem, Classification):
            return ArgTree(num_classes=problem.class_count)
    else:
        return None
        warnings.warn(f"get_model_argtree: Unknown model type {model_class}")


# Trainer/Evaluator ################################################################################

def get_trainer_args(trainer_extension_fs, dataset):
    from vidlu.modules import losses

    problem = get_problem_from_dataset(dataset)
    args = dict()
    if isinstance(problem, (Classification, SemanticSegmentation)):
        args.update(loss=losses.NLLLossWithLogits(ignore_index=-1))
    return args


# Metrics ##########################################################################################

def get_metrics(trainer, problem):
    from vidlu import metrics

    ret = [partial(metrics.MaxMultiMetric, name_filter=lambda k: k.startswith('mem')),
           partial(metrics.HarmonicMeanMultiMetric, name_filter=lambda k: k.startswith('freq'))]

    if isinstance(problem, (Classification, SemanticSegmentation)):
        clf_metric_names = ('A', 'mIoU', 'IoU') if isinstance(problem, SemanticSegmentation) else ('A',)
        hard_prediction_name = "other_outputs.hard_prediction"
        ret.append(partial(metrics.AverageMultiMetric, name_filter=lambda k: k.startswith('loss')))
        if any(isinstance(e, t.AdversarialTraining) for e in trainer.extensions):
            ret.append(partial(metrics.with_suffix(metrics.ClassificationMetrics, 'adv'),
                               hard_prediction_name="other_outputs_adv.hard_prediction",
                               class_count=problem.class_count, metrics=clf_metric_names))
        elif any(isinstance(e, t.SemiSupervisedVAT) for e in trainer.extensions):
            hard_prediction_name = "other_outputs_l.hard_prediction"
        ret.append(partial(metrics.ClassificationMetrics, hard_prediction_name=hard_prediction_name,
                           class_count=problem.class_count, metrics=clf_metric_names))
    elif isinstance(problem, DepthRegression):
        raise NotImplementedError()
        # ret = [metrics.MeanSquaredError, metrics.MeanAbsoluteError]
    else:
        warnings.warn(f"get_metrics: There are no default metrics for problem {type(problem)}.")
        ret = []
    return ret
