from functools import partial

import torch

import vidlu.optim as vo
import vidlu.modules.inputwise as vmi
from vidlu.modules import losses
from vidlu.training.robustness import attacks

from vidlu.training.robustness import perturbation

# Adversarial attacks

madry_cifar10_attack = partial(attacks.PGDAttack,
                               eps=8 / 255,
                               step_size=2 / 255,
                               step_count=10,  # TODO: change
                               clip_bounds=(0, 1))

channelwise_pgd_attack = partial(attacks.PertModelAttack,
                                 pert_model_f=partial(vmi.Additive, ()),
                                 pert_model_init=None,
                                 step_size=2 / 255,
                                 step_count=10,  # TODO: change
                                 clip_bounds=(0, 1),
                                 projection=0.1)

pmodel_attack_1 = partial(attacks.PertModelAttack,
                          pert_model_f=partial(vmi.AlterGamma, (2, 3)),
                          pert_model_init=None,
                          step_size=2 / 255,
                          step_count=10,  # TODO: change
                          clip_bounds=(0, 1),
                          projection=0.1)

warp_attack = partial(attacks.PertModelAttack,
                      pert_model_f=vmi.Warp,
                      pert_model_init=None,
                      step_size=0.25,
                      step_count=7,  # TODO: change
                      clip_bounds=(0, 1),
                      projection=1.)  # TODO: semantic segmentation

entmin_attack = partial(madry_cifar10_attack,
                        minimize=False,
                        loss=lambda logits, _: losses.entropy_l(logits))

virtual_pgd_cifar10_attack = partial(madry_cifar10_attack,
                                     to_virtual_target='argmax')

vat_pgd_cifar10_attack = partial(madry_cifar10_attack,
                                 to_virtual_target='probs',
                                 loss=losses.kl_div_l)
0
mnistnet_tent_eval_attack = partial(attacks.PGDAttack,
                                    eps=0.3,
                                    step_size=0.1,
                                    grad_processing='sign',
                                    step_count=40,  # TODO: change
                                    clip_bounds=(0, 1),
                                    stop_on_success=True)

morsic_tps_warp_attack = partial(attacks.PertModelAttack,
                                 # pert_model_f=partial(vmi.MorsicTPSWarp, grid_shape=(2, 2),
                                 #                      label_padding_mode='zeros'),
                                 pert_model_f=partial(perturbation.OrsicPhotometricAndTPS),
                                 # pert_model_init=lambda pmodel: pmodel.theta.uniform_(-.1, .1),
                                 pert_model_init=lambda pmodel: vmi.reset_parameters(pmodel),
                                 step_size=0.01,
                                 step_count=7,
                                 projection=.2)  # TODO: semantic segmentation


def get_standard_pert_modeL_attack_params(param_to_bounds, param_to_initialization_params=None,
                                          param_to_step_size=None, step_size_factor=None,
                                          initializer_f=perturbation.LInfBallUniformInitializer,
                                          projection_f=perturbation.ClampProjection):
    if (param_to_step_size is None) == (step_size_factor is None):
        raise RuntimeError("Either param_to_step_size or step_size should be provided.")
    if param_to_initialization_params is None:
        param_to_initialization_params = param_to_bounds
    if param_to_step_size is None:
        param_to_step_size = {k: (v[1] - v[0]) * step_size_factor for k, v in
                              param_to_bounds.items()}
    return dict(step_size=param_to_step_size,
                initializer=initializer_f(param_to_initialization_params),
                projection=projection_f(param_to_bounds))


def get_channel_gamma_hsv_attack_params(log_gamma_bounds=(-0.4, 0.4), hsv_addend_bounds=(-0.1, 0.1),
                                        step_size_factor=1 / 8):
    param_to_bounds = {'module.gamma.log_gamma': log_gamma_bounds,
                       'module.additive.addend': hsv_addend_bounds}
    return get_standard_pert_modeL_attack_params(param_to_bounds, step_size_factor=step_size_factor)


channel_gamma_hsv_attack = partial(
    attacks.PertModelAttack,
    optim_f=partial(vo.ProcessedGradientDescent, process_grad=torch.sign),
    pert_model_f=perturbation.ChannelGammaHsv,
    **get_channel_gamma_hsv_attack_params())

tps_warp_attack = partial(
    attacks.PertModelAttack,
    optim_f=partial(vo.ProcessedGradientDescent, process_grad=torch.sign),
    pert_model_f=partial(vmi.BackwardTPSWarp, control_grid_shape=(2, 2)),
    step_size=0.01,  # 0.01 the image height/width
    initializer=perturbation.NormalInitializer({'offsets': (0, 0.1)}),
    projection=perturbation.ScalingProjection({'offsets': 0.1}, p=2, dim=-1))

import vidlu.transforms.jitter as vtj


class BatchRandAugment:
    def __init__(self, m, n):
        self.base = vtj.RandAugment(m, n)

    def __call__(self, input):
        return torch.stack([self.base((x, None))[0] for x in input])


randaugment = partial(
    attacks.PertModelAttack,
    optim_f=partial(vo.ProcessedGradientDescent, process_grad=torch.sign),
    pert_model_f=partial(
        lambda *a, **k: vmi.PertModel(BatchRandAugment(3, 4), forward_arg_count=1)),
    step_size=0)

tps_warp_attack_weaker = partial(
    attacks.PertModelAttack,
    optim_f=partial(vo.ProcessedGradientDescent, process_grad=torch.sign),
    pert_model_f=partial(vmi.BackwardTPSWarp, control_grid_shape=(2, 2)),
    step_size=0.01,  # 0.01 the image height/width
    initializer=perturbation.NormalInitializer({'offsets': (0, 0.03)}),
    projection=perturbation.ScalingProjection({'offsets': 0.03}, p=2, dim=-1))