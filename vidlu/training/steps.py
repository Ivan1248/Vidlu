from contextlib import suppress as ctx_suppress
from functools import lru_cache, partial

import torch

from vidlu.data import BatchTuple
from vidlu.utils.collections import NameDict
from vidlu.utils.torch import (concatenate_tensors_trees, switch_training,
                               batchnorm_stats_tracking_off)


# Training/evaluation steps ########################################################################


## Supervised

def do_optimization_step(optimizer, loss):
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()


@torch.no_grad()
def supervised_eval_step(trainer, batch):
    trainer.model.eval()
    x, y = trainer.prepare_batch(batch)
    output, other_outputs = trainer.extend_output(trainer.model(x))
    loss = trainer.loss(output, y)
    return NameDict(x=x, target=y, output=output, other_outputs=other_outputs, loss=loss.item())


def _supervised_train_step_x_y(trainer, x, y):
    trainer.model.train()
    output, other_outputs = trainer.extend_output(trainer.model(x))
    loss = trainer.loss(output, y)
    do_optimization_step(trainer.optimizer, loss)
    return NameDict(x=x, target=y, output=output, other_outputs=other_outputs, loss=loss.item())


def supervised_train_step(trainer, batch):
    return _supervised_train_step_x_y(trainer, *trainer.prepare_batch(batch))


## Supervised multistep

class SupervisedTrainMultiStep:
    """A training step where each batch is used multiple times consecutively.

    Args:
        repeat_count (int): Number of times each batch is repeated
            consecutively.
    """

    def __init__(self, repeat_count):
        self.repeat_count = repeat_count

    def __call__(self, trainer, batch):
        trainer.model.train()
        for i in range(self.repeat_count):
            with batchnorm_stats_tracking_off(trainer.model) if i == 0 else ctx_suppress():
                x, y = trainer.prepare_batch(batch)
                output, other_outputs = trainer.extend_output(trainer.model(x))
                loss = trainer.loss(output, y)
                do_optimization_step(trainer.optimizer, loss)
                if i == 0:
                    initial = dict(output=output, other_outputs=other_outputs, loss=loss.item())
        final = dict(output=output, other_outputs=other_outputs, loss=loss.item())
        return NameDict(x=x, target=y, **initial, **{f"{k}_post": v for k, v in final.items()})


class SupervisedSlidingBatchTrainStep:
    """A training step where the batch slides through training examples with a
    stride smaller than batch size, making each example used
    `batch_size//stride` times consecutively. but in batches differing in
    `2*stride` examples (`stride` examples removed from the beginning, `stride` examples
    added to the end).

    This should hopefully reduce overfitting with respect to
    `SupervisedTrainMultiStep`, where each batch is repeated `repeat_count`
    (equivalent to `steps_per_batch` here).

    Args:
        stride (int): Number of examples tha batch is shifted in each substep.
        steps_per_batch (int): Number of times the batch is shifted per per the
            length of a batch. If this is provided, `stride` is automatically
            computed as `stride=batch_size//steps_per_batch`.
        reversed (bool, True): Use same batches but in reversed order in each
            multi-step. This is True by default because it enables more variety
            between consecutive batches during training and doesn't make the
            returned predictions too optimistic.

    The arguments `stride` and `steps_per_batch` are mutually exclusive, i.e.
    only 1 has to be provided.
    """

    def __init__(self, steps_per_batch=None, stride=None, reversed=True):
        self.prev_x_y = None
        self.start_index = 0
        if (stride is None) == (steps_per_batch is None):
            raise ValueError("Either `stride` or `steps_per_batch` should be provided.")
        self.steps_per_batch = steps_per_batch
        self.stride = stride
        self.reversed = reversed

    def __call__(self, trainer, batch):
        trainer.model.train()
        n = len(batch[0])
        stride = self.stride or n // self.steps_per_batch
        x_y = trainer.prepare_batch(batch)

        if self.prev_x_y is None:  # the first batch
            self.prev_x_y = x_y
            self.start_index = stride
            return _supervised_train_step_x_y(trainer, *x_y)

        x, y = [torch.cat([a, b], dim=0) for a, b in zip(self.prev_x_y, x_y)]
        starts = list(range(self.start_index, len(x) - n + 1, stride))
        if self.reversed:
            starts = list(reversed(starts))
        result = None
        for i in starts:
            with batchnorm_stats_tracking_off(trainer.model) if i == starts[0] else ctx_suppress():
                inter_x, inter_y = [a[i:i + n] for a in (x, y)]
                output, other_outputs = trainer.extend_output(trainer.model(inter_x))
                loss = trainer.loss(output, inter_y)
                do_optimization_step(trainer.optimizer, loss)
                result = result or NameDict(x=inter_x, target=inter_y, output=output,
                                            other_outputs=other_outputs, loss=loss.item())
        self.start_index = starts[0 if self.reversed else -1] + stride - len(self.prev_x_y[0])
        # Data for the last inter-batch iteration is returned
        # The last inter-batch is not necessarily `batch`
        # WARNING: Training performance might be too optimistic if reversed=False
        return result


## Supervised accumulated batch

class SupervisedTrainAcummulatedBatchStep:
    def __init__(self, batch_split_factor, reduction='mean'):
        super().__init__()
        self.batch_split_factor = batch_split_factor
        self.reduction = reduction
        if reduction not in ['mean', 'sum']:
            raise ValueError("reduction is not in {'mean', 'sum'}.")

    def __call__(self, trainer, batch):
        if batch.shape(0) % self.batch_split_factor != 0:
            raise RuntimeError(f"Batch size ({batch.shape(0)}) is not a multiple"
                               + f" of batch_split_factor ({self.batch_split_factor}).")
        trainer.model.train()
        trainer.optimizer.zero_grad()
        x, y = trainer.prepare_batch(batch)
        xs, ys = [b.split(len(x) // self.batch_split_factor, dim=0) for b in [x, y]]
        outputs, other_outputses = [], []
        total_loss = None
        for x, y in zip(xs, ys):
            output, other = trainer.extend_output(trainer.model(x))
            outputs.append(output)
            other_outputses.append(other_outputses)
            loss = trainer.loss(output, y)
            if self.reduction == 'mean':
                loss /= len(x)
            loss.backward()
            if total_loss is None:
                total_loss = loss.detach().clone()
            else:
                total_loss += loss.detach()
        trainer.optimizer.step()
        return NameDict(x=x, target=y, output=torch.cat(outputs, dim=0),
                        other_outputs=concatenate_tensors_trees(other_outputses),
                        loss=total_loss.item())


# Adversarial

class CleanResultCallback:
    def __init__(self, extend_output):
        self.extend_output = extend_output
        self.result = None

    def __call__(self, r):
        if self.result is None:
            self.result = NameDict({
                **vars(r),
                **dict(zip(("output", "other_outputs"), self.extend_output(r.output)))})


def first_output_callback(output_ref: list):
    def backward_callback(r):
        nonlocal output_ref
        if len(output_ref) == 0:
            output_ref.append(r)


class AdversarialTrainStep:
    """ Adversarial training step with the option to keep a the batch partially
    clean.

    It calls `trainer.model.eval()`, `trainer.attack.perturb` with
    `1 - clean_proportion` of input examples, and the corresponding labels
    if `virtual` is `False`. Then makes a training step with adversarial
    examples mixed with clean examples, depending on `clean_proportion`.

    'mean' reduction is assumed for the loss.


    Args:
        clean_proportion (float): The proportion of input examples that should
            not be turned into adversarial examples in each step. Default: 0.
        virtual (bool): A value determining whether virtual adversarial examples
            should be used (using the predicted label).
    """

    def __init__(self, clean_proportion=0, virtual=False):
        self.clean_proportion = clean_proportion
        self.virtual = virtual

    def __call__(self, trainer, batch):
        x, y = trainer.prepare_batch(batch)
        cln_count = round(self.clean_proportion * len(x))
        cln_proportion = cln_count / len(x)
        split = lambda a: (a[:cln_count], a[cln_count:])
        (x_c, x_a), (y_c, y_a) = split(x), split(y)

        crc = CleanResultCallback(trainer.extend_output)
        trainer.model.eval()  # adversarial examples are generated in eval mode
        x_adv = trainer.attack.perturb(trainer.model, x_a, None if self.virtual else y[cln_count:],
                                       backward_callback=crc)

        trainer.model.train()
        output, other_outputs = trainer.extend_output(trainer.model(torch.cat((x_c, x_adv), dim=0)))
        output_c, output_adv = split(output)
        loss_adv = trainer.loss(output_adv, y_a)
        loss_c = trainer.loss(output_c, y_c) if len(y_c) > 0 else 0
        do_optimization_step(trainer.optimizer,
                             loss=cln_proportion * loss_c + (1 - cln_proportion) * loss_adv)

        other_outputs_adv = NameDict({k: a[cln_count:] for k, a in other_outputs.items()})
        return NameDict(x=x, output=crc.result.output, target=y,
                        other_outputs=crc.result.other_outputs, loss=crc.result.loss,
                        x_adv=x_adv, target_adv=y_a, output_adv=output_adv,
                        other_outputs_adv=other_outputs_adv, loss_adv=loss_adv.item())


class AdversarialCombinedLossTrainStep:
    """A training step that performs an optimization on an weighted
    combination of the standard loss and the adversarial loss.

    Args:
        use_attack_loss (bool): A value determining whether the loss function
            from the attack (`trainer.attack.loss`) should be used as the
            adversarial loss instead of the standard loss function
            (`trainer.loss`).
        clean_weight (float): The weight of the clean loss, a number
            normally between 0 and 1. Default: 0.5.
        adv_weight (float): The weight of the clean loss, a number normally
            between 0 and 1. If no value is provided (`None`), it is computed as
            `1 - clean_loss_weight`.
        virtual (bool): A value determining whether virtual adversarial examples
            should be used (using the predicted label).
    """

    def __init__(self, use_attack_loss, clean_weight=0.5, adv_weight=None, virtual=False):
        self.use_attack_loss = use_attack_loss
        self.clean_loss_weight = clean_weight
        self.adv_loss_weight = 1 - clean_weight if adv_weight is None else adv_weight
        self.virtual = virtual

    def __call__(self, trainer, batch):
        x, y = trainer.prepare_batch(batch)

        trainer.model.eval()  # adversarial examples are generated in eval mode
        x_adv = trainer.attack.perturb(x, None if self.virtual else y)

        trainer.model.train()
        output_c, other_outputs_c = trainer.extend_output(trainer.model(x))
        loss_c = trainer.loss(output_c, y)
        output_adv, other_outputs_adv = trainer.extend_output(trainer.model(x_adv))
        loss_adv = (trainer.attack if self.use_attack_loss else trainer).loss(output_adv, y)
        do_optimization_step(trainer.optimizer,
                             loss=self.clean_loss_weight * loss_c + self.adv_loss_weight * loss_adv)

        return NameDict(x=x, output=output_c, target=y, other_outputs=other_outputs_c, loss=loss_c,
                        x_adv=x_adv, output_adv=output_adv, other_outputs_adv=other_outputs_adv,
                        loss_adv=loss_adv.item())


class AdversarialTrainBiStep:
    """A training step that first performs an optimization step on a clean batch
    and then on the batch turned into adversarial examples.

    Args:
        virtual (bool): A value determining whether virtual adversarial examples
            should be used (using the predicted label).
    """

    def __init__(self, virtual=False):
        self.virtual = virtual

    def __call__(self, trainer, batch):
        x, y = trainer.prepare_batch(batch)
        clean_result = _supervised_train_step_x_y(trainer, x, y)
        trainer.model.eval()  # adversarial examples are generated in eval mode
        x_adv = trainer.attack.perturb(trainer.model, x, None if self.virtual else y)
        result_adv = _supervised_train_step_x_y(trainer, x_adv, y)
        return NameDict(x=x, output=clean_result.output, target=y,
                        other_outputs=clean_result.other_outputs, loss=clean_result.loss,
                        x_adv=x_adv, output_adv=result_adv.output,
                        other_outputs_adv=result_adv.other_outputs, loss_adv=result_adv.loss)


class AdversarialTrainMultiStep:
    def __init__(self, update_period=1, virtual=False, train_mode=True,
                 reuse_perturbation=False):
        self.update_period = update_period
        self.virtual = virtual
        self.train_mode = train_mode
        self.reuse_perturbation = reuse_perturbation
        self._perturbation = None

    def __call__(self, trainer, batch):
        # assert trainer.attack.loss == trainer.loss
        x, y = trainer.prepare_batch(batch)
        clean_result = None

        def step(r):
            nonlocal clean_result, x, self
            if clean_result is None:
                clean_result = r
            if r.step % self.update_period == 0:
                trainer.optimizer.step()
                trainer.optimizer.zero_grad()

        (trainer.model.train if self.train_mode else trainer.model.eval)()
        with batchnorm_stats_tracking_off(trainer.model) if self.train_mode else ctx_suppress():
            perturb = trainer.attack.perturb
            if self.reuse_perturbation and (
                    self._perturbation is None or self._perturbation.shape == x.shape):
                perturb = partial(perturb, delta_init=self._perturbation)
            x_adv = perturb(trainer.model, x, None if self.virtual else y, backward_callback=step)
            if self.reuse_perturbation:
                self._perturbation = x_adv - x

        trainer.model.train()
        output_adv, other_outputs_adv = trainer.extend_output(trainer.model(x_adv))
        loss_adv = trainer.loss(output_adv, y)
        do_optimization_step(trainer.optimizer, loss_adv)

        return NameDict(x=x, target=y, output=clean_result.output,
                        other_outputs=trainer.extend_output(clean_result.output)[1],
                        loss=clean_result.loss, x_adv=x_adv, output_adv=output_adv,
                        other_outputs_adv=other_outputs_adv, loss_adv=loss_adv.item())


class VATTrainStep:
    def __init__(self, alpha=1):
        self.alpha = alpha

    def __call__(self, trainer, batch):
        x, y = trainer.prepare_batch(batch)

        trainer.model.train()
        output, other_outputs = trainer.extend_output(trainer.model(x))
        loss = trainer.loss(output, y)

        with batchnorm_stats_tracking_off(trainer.model):
            x_adv = trainer.attack.perturb(trainer.model, x, output)
            output_adv, other_outputs_adv = trainer.extend_output(trainer.model(x_adv))
            loss_adv = self.alpha * trainer.attack.loss(output_adv, y)
            do_optimization_step(trainer.optimizer, loss=loss + self.alpha * loss_adv)

        return NameDict(x=x, target=y, output=output, other_outputs=other_outputs, loss=loss.item(),
                        x_adv=x_adv, output_adv=output_adv, other_outputs_adv=other_outputs_adv,
                        loss_adv=loss_adv.item())


@torch.no_grad()
def _prepare_semisupervised_vat_input(trainer, batch):
    if isinstance(batch, BatchTuple):
        (x_l, y_l), (x_u, *_) = [trainer.prepare_batch(b) for b in batch]
        x = torch.cat([x_l, x_u], 0)
    else:
        x_l, y_l = trainer.prepare_batch(batch)
        x = x_l
    return x, x_l, y_l, len(x_l)


def semisupervised_vat_eval_step(trainer, batch):
    trainer.model.eval()

    x, x_l, y_l, n_l = _prepare_semisupervised_vat_input(trainer, batch)

    output, other_outputs = trainer.extend_output(trainer.model(x))
    x_adv = trainer.attack.perturb(trainer.model, x, other_outputs.probs)
    output_adv, other_outputs_adv = trainer.extend_output(trainer.model(x_adv))
    loss_adv = trainer.attack.loss(output_adv, other_outputs.probs.detach())

    output_l = output[:n_l]
    loss_l = trainer.loss(output_l, y_l)

    other_outputs_l = type(other_outputs)({k: v[:n_l] for k, v in other_outputs.items()})
    return NameDict(x=x, output=output, other_outputs=other_outputs, output_l=output_l,
                    other_outputs_l=other_outputs_l, loss_l=loss_l.item(), x_adv=x_adv,
                    loss_adv=loss_adv.item(), x_l=x_l, target=y_l, output_adv=output_adv,
                    other_outputs_adv=other_outputs_adv)


class SemisupervisedVATTrainStep:
    def __init__(self, alpha=1, attack_eval_model=False):
        self.alpha = alpha
        self.attack_eval_model = attack_eval_model

    def __call__(self, trainer, batch):
        model = trainer.model
        model.train()

        x, x_l, y_l, n_l = _prepare_semisupervised_vat_input(trainer, batch)
        x = x[:n_l]  # TODO: remove

        output, other_outputs = trainer.extend_output(model(x))

        with switch_training(model, False) if self.attack_eval_model else ctx_suppress():
            with batchnorm_stats_tracking_off(model) if model.training else ctx_suppress():
                x_adv = trainer.attack.perturb(model, x, other_outputs.probs)
                # NOTE: putting this outside of batchnorm_stats_tracking_off harms learning:
                output_adv, other_outputs_adv = trainer.extend_output(model(x_adv))
            loss_adv = trainer.attack.loss(output_adv, other_outputs.probs.detach())

        output_l = output[:n_l]
        loss_l = trainer.loss(output_l, y_l)
        do_optimization_step(trainer.optimizer, loss=loss_l + self.alpha * loss_adv)

        other_outputs_l = type(other_outputs)({k: v[:n_l] for k, v in other_outputs.items()})
        return NameDict(x=x, output=output, other_outputs=other_outputs, output_l=output_l,
                        other_outputs_l=other_outputs_l, loss_l=loss_l.item(),
                        loss_adv=loss_adv.item(), x_l=x_l, target=y_l, x_adv=x_adv,
                        output_adv=output_adv, other_outputs_adv=other_outputs_adv)


def adversarial_eval_step(trainer, batch):
    trainer.model.eval()

    x, y = trainer.prepare_batch(batch)

    with torch.no_grad():
        output, other_outputs = trainer.extend_output(trainer.model(x))
        loss = trainer.loss(output, y)

    x_adv = trainer.eval_attack.perturb(trainer.model, x, y)
    with torch.no_grad():
        output_adv, other_outputs_adv = trainer.extend_output(trainer.model(x_adv))
        loss_adv = trainer.loss(output_adv, y)

    return NameDict(x=x, target=y, output=output, other_outputs=other_outputs, loss=loss.item(),
                    x_adv=x_adv, output_adv=output_adv, other_outputs_adv=other_outputs_adv,
                    loss_adv=loss_adv.item())


class AdversarialTargetedEvalStep:
    def __init__(self, targets=None, class_count=None):
        if (targets is None) == (class_count is None):
            raise RuntimeError("Either the `targets` or the `class_count`"
                               + " argument needs to be provided.")
        self.targets = list(range(class_count)) if targets is None else targets

    def __call__(self, trainer, batch):
        trainer.model.eval()

        x, y = trainer.prepare_batch(batch)

        with torch.no_grad():
            output, other_outputs = trainer.extend_output(trainer.model(x))
            loss = trainer.loss(output, y)

        for i, t in enumerate(self.targets):
            t_var = torch.full_like(y, t)
            x_adv = trainer.eval_attack.perturb(trainer.model, x, t_var)
            result = dict()
            with torch.no_grad():
                result[f"output_adv{i}"], result[f"other_outputs_adv{i}"] = trainer.extend_output(trainer.model(x_adv))
                result[f"loss_adv_targ{i}"] = trainer.loss(result[f"output_adv{i}"], t_var).item()
                result[f"loss_adv{i}"] = trainer.loss(result[f"output_adv{i}"], y).item()

        return NameDict(x=x, target=y, output=output, other_outputs=other_outputs, loss=loss.item(),
                        x_adv=x_adv, **result)


def adversarial_targeted_eval_step(trainer, batch):
    trainer.model.eval()

    x, y = trainer.prepare_batch(batch)

    with torch.no_grad():
        output, other_outputs = trainer.extend_output(trainer.model(x))
        loss = trainer.loss(output, y)

    x_adv = trainer.eval_attack.perturb(trainer.model, x, y)
    with torch.no_grad():
        output_adv, other_outputs_adv = trainer.extend_output(trainer.model(x_adv))
        loss_adv = trainer.loss(output_adv, y)

    return NameDict(x=x, target=y, output=output, other_outputs=other_outputs, loss=loss.item(),
                    x_adv=x_adv, output_adv=output_adv, other_outputs_adv=other_outputs_adv,
                    loss_adv=loss_adv.item())


# Autoencoder

def autoencoder_train_step(trainer, batch):
    trainer.model.train()
    x = trainer.prepare_batch(batch)[0]
    x_r = trainer.model(x)
    loss = trainer.loss(x_r, x)
    do_optimization_step(trainer.optimizer, loss)
    return NameDict(x_r=x_r, x=x, loss=loss.item())


# GAN

class GANTrainStep:
    @lru_cache(1)
    def _get_real_labels(self, batch_size):
        return torch.ones(batch_size, device=self.model.device)

    @lru_cache(1)
    def _get_fake_labels(self, batch_size):
        return torch.zeros(batch_size, device=self.model.device)

    def __call__(self, trainer, batch):
        """ Copied from ignite/examples/gan/dcgan and modified"""
        trainer.model.train()
        real = trainer.prepare_batch(batch)[0]
        batch_size = real.shape[0]
        real_labels = trainer._get_real_labels(batch_size)

        # (1) Update D network: maximize log(D(x)) + log(1 - D(G(z))) ##########
        discriminator, generator = trainer.model.discriminator, trainer.model.generator
        discriminator.zero_grad()

        # training discriminator with real
        output = discriminator(real)
        errD_real = trainer.loss(output, real_labels)  # torch.nn.BCELoss()
        D_real = output.mean().item()
        errD_real.backward()

        fake = generator(trainer.model.sample_z(batch_size))

        # training discriminator with fake
        output = discriminator(fake.detach())
        errD_fake = trainer.loss(output, trainer._get_fake_labels(batch_size))
        D_fake1 = output.mean().item()
        errD_fake.backward()

        trainer.optimizer['D'].step()

        # (2) Update G network: maximize log(D(G(z))) ##########################
        generator.zero_grad()

        # Update generator. We want to make a step that will make it more likely that D outputs "real"
        output = discriminator(fake)
        errG = trainer.loss(output, real_labels)
        D_fake2 = output.mean().item()

        errG.backward()
        trainer.optimizer['G'].step()

        return NameDict(errD=(errD_real + errD_fake).item(), errG=errG.item(), D_real=D_real,
                        D_fake1=D_fake1, D_fake2=D_fake2)