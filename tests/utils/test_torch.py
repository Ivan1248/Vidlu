import torch


class TestSaveGrads:
    def test_save_grads(self):
        from vidlu.utils.torch import save_grads

        x = torch.Tensor([1])
        x.requires_grad_()
        (x ** 2).backward()

        pre_grad = x.grad.clone().detach()
        with save_grads([x]):
            for _ in range(5):
                (x ** 2).backward()
                assert torch.all(x.grad != pre_grad)
        assert torch.all(x.grad == pre_grad)
