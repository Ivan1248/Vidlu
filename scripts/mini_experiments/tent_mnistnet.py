from functools import partial
import tempfile
from pathlib import Path

import torch
from torch import optim, nn
from torch.utils.data import DataLoader

from _context import vidlu
from vidlu.data.datasets import MNIST
from vidlu import models
from vidlu import factories
from vidlu.modules import components
from vidlu.modules.other.mnistnet import MNISTNetBackbone
from vidlu.training import AdversarialTrainer, configs, adversarial, initialization, metrics
from vidlu.utils import misc, indent_print, logger

# Data
data_dir = Path(tempfile.gettempdir()) / 'datasets'
data_dir.mkdir(exist_ok=True)
data_dir = data_dir / 'mnist'
data = dict(train=MNIST(data_dir, 'trainval'), test=MNIST(data_dir, 'test'))
prepare = factories.get_data_preparation(data['train'])  # convert to torch and [0, 1] range
data = dict(**{k: prepare(v) for k, v in data.items()})


# Model
class MNISTNetTentModel(models.SeqModel):
    def __init__(self):
        super().__init__(
            seq=dict(
                backbone=MNISTNetBackbone(
                    act_f=partial(components.Tent, channelwise=False, delta_range=(0.05, 1.)),
                    use_bn=True),
                head=components.heads.ClassificationHead1D(10)),
            init=initialization.kaiming_mnistnet)


model = MNISTNetTentModel()
model(next(iter(DataLoader(data['train'], batch_size=2)))[0])  # infer input dimensions
model.to(device='cuda:0')


# model.initialize()


# Trainer

def create_optimizer(trainer):
    delta_params = [v for k, v in model.named_parameters() if k.endswith('delta')]
    other_params = [v for k, v in model.named_parameters() if not k.endswith('delta')]
    return optim.Adam([dict(params=other_params), dict(params=delta_params, weight_decay=0.12)],
                      lr=1e-3, weight_decay=1e-6)


trainer = AdversarialTrainer(
    model=model,
    extend_output=configs.classification_extend_output,
    loss_f=nn.CrossEntropyLoss,
    train_step=configs.AdversarialTrainStep(),
    # no attack is used during training (the training is not adversarial)
    attack_f=adversarial.attacks.DummyAttack,
    eval_step=configs.adversarial_eval_step,
    eval_attack_f=partial(adversarial.attacks.PGDAttack, eps=0.3, step_size=0.1, step_count=20,
                          stop_on_success=True),
    epoch_count=40,
    batch_size=100,
    optimizer_maker=create_optimizer)

for m in [metrics.FuncMetric(lambda iter_output: iter_output.loss, name='loss'),
          metrics.ClassificationMetrics(10, metrics=['A']),
          metrics.with_suffix(metrics.ClassificationMetrics, 'adv')(
              10, hard_prediction_name="other_outputs_adv.hard_prediction", metrics=['A'])]:
    trainer.add_metric(m)


# Reporting and evaluation during training

def define_training_loop_actions(trainer, data, logger):
    @trainer.training.epoch_started.add_handler
    def on_epoch_started(es):
        logger.log(f"Starting epoch {es.epoch}/{es.max_epochs}"
                   + f" ({es.batch_count} batches, lr={', '.join(f'{x:.2e}' for x in trainer.lr_scheduler.get_lr())})")

    @trainer.training.epoch_completed.add_handler
    def on_epoch_completed(_):
        """Evaluation on the test (validation) set after each epoch."""
        trainer.eval(data['test'])

    def report_metrics(es, is_validation=False):
        def eval_str(metrics):
            return ', '.join([f"{k}={v:.4f}" for k, v in metrics.items()])

        metrics = trainer.get_metric_values(reset=True)
        with indent_print.indent_print():
            epoch_fmt, iter_fmt = f'{len(str(es.max_epochs))}d', f'{len(str(es.batch_count))}d'
            iter = es.iteration % es.batch_count
            prefix = ('val' if is_validation
                      else f'{format(es.epoch, epoch_fmt)}.'
                           + f'{format((iter - 1) % es.batch_count + 1, iter_fmt)}')
            logger.log(f"{prefix}: {eval_str(metrics)}")

    @trainer.evaluation.iteration_completed.add_handler
    def on_eval_iteration_completed(state):
        """This is for interaction during training and evaluation, e.g. by
        entering "embed()" followed by ENTER while training opens the IPython
        shell.
        """
        nonlocal trainer, data
        from IPython import embed

        optional_input = misc.try_input()
        if optional_input is not None:
            try:
                exec(optional_input)
            except Exception as ex:
                print(f'Cannot execute "{optional_input}"\n{ex}.')

    @trainer.training.iteration_completed.add_handler
    def on_iteration_completed(s):
        if s.iteration % s.batch_count % (max(1, s.batch_count // 5)) == 0:
            remaining = s.batch_count - s.iteration % s.batch_count
            if remaining >= s.batch_count // 5 or remaining == 0:
                report_metrics(s)

        on_eval_iteration_completed(s)

    trainer.evaluation.epoch_completed.add_handler(partial(report_metrics, is_validation=True))


logger = logger.Logger()
define_training_loop_actions(trainer, data, logger)

trainer.eval(data['test'])
trainer.train(data['train'])
trainer.eval(data['train'])
