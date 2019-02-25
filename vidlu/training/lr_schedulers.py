from functools import partial

from torch.optim.lr_scheduler import MultiStepLR, LambdaLR


class ScalableMultiStepLR(MultiStepLR):
    """Set the learning rate of each parameter group to the initial lr decayed
    by gamma once the number of epoch reaches one of the milestones. When
    last_epoch=-1, sets initial lr as lr.

    Args:
        optimizer (Optimizer): Wrapped optimizer.
        milestones (list): List of numbers from the interval (0, 1). Must be
            increasing. Milestone epoch indices are calculated by multiplying
            provided milestones with the total number of epochs (`epoch_count`).
        epoch_count: The total number of epochs.
        gamma (float): Multiplicative factor of learning rate decay.
            Default: 0.1.
        last_epoch (int): The index of last epoch. Default: -1.

    Example:
        >>> # Assuming optimizer uses lr = 0.05 for all groups
        >>> # lr = 0.05     if epoch < 30
        >>> # lr = 0.005    if 30 <= epoch < 80
        >>> # lr = 0.0005   if epoch >= 80
        >>> epoch_count = 100
        >>> scheduler = ScalableMultiStepLR(optimizer, milestones=[0.3, 0.8],
        ...                               epoch_count=epoch_count, gamma=0.1)
        >>> for epoch in range(epoch_count):
        >>>     scheduler.step()
        >>>     train(...)
        >>>     validate(...)
    """

    def __init__(self, optimizer, milestones, epoch_count, gamma=0.1, last_epoch=-1):
        super().__init__(optimizer, milestones=[round(m * epoch_count) for m in milestones],
                         gamma=gamma, last_epoch=last_epoch)


class ScalableLambdaLR(LambdaLR):
    """Sets the learning rate of each parameter group to the initial lr
    times a given function. When last_epoch=-1, sets initial lr as lr.

    Args:
        optimizer (Optimizer): Wrapped optimizer.
        lr_lambda (function or list): A function which computes a multiplicative
            factor from the epoch index divided by the total number of epochs (a
            number between 0 and 1), or a list of such functions, one for each
            group in optimizer.param_groups.
        epoch_count: The total number of epochs.
        last_epoch (int): The index of last epoch. Default: -1.

    Example:
        >>> # Assuming optimizer has two groups.
        >>> epoch_count = 100
        >>> lambda1 = lambda progress: 1/30 * progress
        >>> lambda2 = lambda progress: 0.95 ** progress
        >>> scheduler = LambdaLR(optimizer, lr_lambda=[lambda1, lambda2], epoch_count=epoch_count)
        >>> for epoch in range(epoch_count):
        >>>     scheduler.step()
        >>>     train(...)
        >>>     validate(...)
    """

    def __init__(self, optimizer, lr_lambda, epoch_count, last_epoch=-1):
        if not isinstance(lr_lambda, list) and not isinstance(lr_lambda, tuple):
            lr_lambda = [lr_lambda] * len(optimizer.param_groups)
        lr_lambda = [lambda e: ll(e / epoch_count) for ll in lr_lambda]
        super().__init__(optimizer=optimizer, lr_lambda=lr_lambda, last_epoch=last_epoch)
