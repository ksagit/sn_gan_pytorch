import torch.nn as nn
import torch.nn.functional as F
import torch
from resnet_layers import GeneratorBlock, SNGeneratorBlock, DiscriminatorBlock
from torch.nn.init import xavier_uniform_
from torch.nn.utils import spectral_norm

from torch.distributions.normal import Normal
from scipy.stats import truncnorm
import numpy as np
from spectral_layers import SNLinear, SNConv2d, SNEmbedId


def sample_z(batch_size, truncate=False, clip=1.5):
    if truncate:
        n = truncnorm.rvs(-clip, clip, size=(batch_size, 128))
        return torch.from_numpy(n).float()
    else:
        n = Normal(torch.tensor([0.0]), torch.tensor([1.0]))
        return n.sample((batch_size, 128)).squeeze(2)

def sample_c(batch_size, n_classes):
    if n_classes == 0:
        return None
    else:
        return torch.randint(low=0, high=n_classes, size=(batch_size,))

class Cifar10Generator(nn.Module):
    
    def __init__(self, z_size=128, bottom_width=4, n_classes=0):
        super(Cifar10Generator, self).__init__()
        
        self.bottom_width = bottom_width
        self.n_classes = n_classes
        
        self.linear_1 = nn.Linear(z_size, (bottom_width ** 2) * 256)
        self.block_1 = GeneratorBlock(256, 256, upsample=True, n_classes=n_classes)
        self.block_2 = GeneratorBlock(256, 256, upsample=True, n_classes=n_classes)
        self.block_3 = GeneratorBlock(256, 256, upsample=True, n_classes=n_classes)
        self.batchnorm = nn.BatchNorm2d(256, eps=2e-5)
        self.conv = nn.Conv2d(256, 3, 3, padding=1)

        xavier_uniform_(self.linear_1.weight)
        xavier_uniform_(self.conv.weight)

        
    def forward(self, z, y=None):
        if y is not None:
            assert(z.shape[0] == y.shape[0])
        else:
            assert(self.n_classes == 0)

        x = self.linear_1(z)
        x = x.view(x.shape[0], -1, self.bottom_width, self.bottom_width)
        
        x = self.block_1(x, y)
        x = self.block_2(x, y)
        x = self.block_3(x, y)
        x = self.batchnorm(x)
        x = F.relu(x)
        x = self.conv(x)
        x = torch.tanh(x)
        return x

class SNCifar10Generator(nn.Module):
    
    def __init__(self, z_size=128, bottom_width=4, n_classes=0):
        super(SNCifar10Generator, self).__init__()
        
        self.bottom_width = bottom_width
        self.n_classes = n_classes
        
        self.linear_1 = SNLinear(z_size, (bottom_width ** 2) * 256)
        self.block_1 = SNGeneratorBlock(256, 256, upsample=True, n_classes=n_classes)
        self.block_2 = SNGeneratorBlock(256, 256, upsample=True, n_classes=n_classes)
        self.block_3 = SNGeneratorBlock(256, 256, upsample=True, n_classes=n_classes)
        self.batchnorm = nn.BatchNorm2d(256, eps=2e-5)
        self.conv = SNConv2d(256, 3, 3, padding=1)

        xavier_uniform_(self.linear_1.weight)
        xavier_uniform_(self.conv.weight)

        
    def forward(self, z, y=None):
        if y is not None:
            assert(z.shape[0] == y.shape[0])
        else:
            assert(self.n_classes == 0)

        x = self.linear_1(z)
        x = x.view(x.shape[0], -1, self.bottom_width, self.bottom_width)
        
        x = self.block_1(x, y)
        x = self.block_2(x, y)
        x = self.block_3(x, y)
        x = self.batchnorm(x)
        x = F.relu(x)
        x = self.conv(x)
        x = torch.tanh(x)
        return x
    
class Cifar10Discriminator(nn.Module):
    
    def __init__(self, channels=128, n_classes=0, use_gamma=False):
        super(Cifar10Discriminator, self).__init__()
        
        self.n_classes = n_classes
        self.use_gamma = use_gamma

        self.block1 = DiscriminatorBlock(3, channels, downsample=True, optimized=True, use_gamma=use_gamma)
        self.block2 = DiscriminatorBlock(channels, channels, downsample=True, use_gamma=use_gamma)
        self.block3 = DiscriminatorBlock(channels, channels, downsample=False, use_gamma=use_gamma)
        self.block4 = DiscriminatorBlock(channels, channels, downsample=False, use_gamma=use_gamma)
        
        self.dense = SNLinear(channels, 1, bias=False, use_gamma=use_gamma) 
        xavier_uniform_(self.dense.weight)

        if n_classes > 0:
            self.class_embedding = SNEmbedId(n_classes, channels)
            xavier_uniform_(self.class_embedding.weight)
        
    def sum_gammas(self):
        if not self.use_gamma:
            raise ValueError('The model is not reparametrized; there are no gammas to sum')
        else:
            gammas = 0
            for block in [self.block1, self.block2, self.block3, self.block4]:
                gammas += block.conv1.gamma + block.conv2.gamma
                if block.learnable_shortcut:
                    gammas += block.shortcut.gamma
            gammas += self.dense.gamma
            return gammas

    def forward(self, x, y=None):
        if y is not None:
            assert(x.shape[0] == y.shape[0])
        else:
            assert(self.n_classes == 0)

        h = self.block1(x)
        h = self.block2(h)
        h = self.block3(h)
        h = self.block4(h)
        h = F.relu(h)

        h = torch.sum(h, dim=(2,3))
        output = self.dense(h)

        if y is not None:
            assert(len(y.shape) == 1)

            label_weights = self.class_embedding(y)
            output += torch.sum(label_weights * h, dim=1, keepdim=True)

        return output