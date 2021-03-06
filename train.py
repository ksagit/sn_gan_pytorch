import logging
import os
os.environ['TF_CPP_MIN_LOG_LEVEL']='2'
import sys
from tqdm import tqdm
import json 

from PIL import Image
import math
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

from cifar10_models import sample_z, sample_c
import sys

sys.path.insert(0, '/home/kyle/')
from evaluate import generate_images, calc_inception_chainer, save_checkpoint


def get_gen_loss_stdgan(dis_fake):
    return F.softplus(-dis_fake).mean(0)

def get_gen_loss_hinge(dis_fake):
    return -dis_fake.mean(0)

def get_gen_loss_wgan(dis_fake):
    return -dis_fake.mean(0)


def get_dis_loss_stdgan(dis_fake, dis_real):
    L1 = F.softplus(dis_fake).mean(0)
    L2 = F.softplus(-dis_real).mean(0)    
    return L1 + L2

def get_dis_loss_hinge(dis_fake, dis_real):
    L1 = F.relu(1 + dis_fake).mean(0)
    L2 = F.relu(1 - dis_real).mean(0)
    return L1 + L2

def get_dis_loss_wgan(dis_fake, dis_real):
    L1 = -dis_real.mean(0)
    L2 = dis_fake.mean(0)
    return L1 + L2


def get_gradient_penalty(x_fake, x_real, device):
    eps = torch.rand(x_real.shape[0], 1, 1, 1).to(device)
    # eps_c = torch.randint(0, 2, size=(data_batch_size,)).to(device)
    x_mid = eps * x_real + (1 - eps) * x_fake
    # y_mid = eps_c * y_real + (1 - eps_c) * y_fake   # this is kind of a hack but how else do you do a GP for a conditional discriminator???

    x_mid.detach()
    x_mid.requires_grad = True
    dis_mid = d(x_mid)
    grad_outputs = torch.ones_like(dis_mid).to(device)
    grads = torch.autograd.grad(
        outputs=dis_mid, 
        inputs=x_mid,
        grad_outputs=grad_outputs,
        create_graph=True, 
        retain_graph=True, 
        only_inputs=True
    )[0]
    gradient_penalty = ((torch.sum(grads**2, dim=(1,2,3))**.5 - 1)**2).mean(0)
    return gradient_penalty

def get_custom_rank_loss(g):
    loss = 0
    for child in g.modules():
        if hasattr(child, 'custom_rank_loss'):
            loss += child.custom_rank_loss()
    return loss

def spectrally_clip_grads(model):
    for child in model.modules():
        if hasattr(child, 'clamp_gradient_spectra'):
            child.clamp_gradient_spectra()

SV_DICT = {
    'block1.conv1': [],
    'block1.conv2': [],
    'block1.shortcut': [],
    'block2.conv1': [],
    'block2.conv2': [],
    'block2.shortcut': [],
    'block3.conv1': [],
    'block3.conv2': [],
    'block4.conv1': [],
    'block4.conv2': []
}

def monitor_grad_singular_values(model):
    for child in model.named_modules():
        name, module = child
        if 'conv' in name or 'shortcut' in name:
            SV_DICT[name].append(
                module.get_grad_singular_values()
            )


def checksum(model):
    return sum(torch.sum(parameter.data) for parameter in model.parameters())


def train(trainingwrapper, dataset):
    d = trainingwrapper.d.train()
    g = trainingwrapper.g.train()
    d_optim = trainingwrapper.d_optim
    g_optim = trainingwrapper.g_optim
    d_scheduler = trainingwrapper.d_scheduler
    g_scheduler = trainingwrapper.g_scheduler
    config = trainingwrapper.config

    data_batch_size = config['data_batch_size']
    noise_batch_size = config['noise_batch_size']
    dis_iters = config['dis_iters']
    max_iters = config['max_iters']
    subsample = config['subsample']
    conditional = config['conditional']
    lam1 = config['lam1']
    lam2 = config['lam2']
    lam3 = config['lam3']

    if config['loss_type'] == 'hinge':
        get_gen_loss = get_gen_loss_hinge
        get_dis_loss = get_dis_loss_hinge
    elif config['loss_type'] == 'stdgan':
        get_gen_loss = get_gen_loss_stdgan
        get_dis_loss = get_dis_loss_stdgan
    elif config['loss_type'] == 'wgan':
        get_gen_loss = get_gen_loss_wgan
        get_dis_loss = get_dis_loss_wgan
    else:
        raise NotImplementedError('Loss type not implemented')

    sn_gan_data_path = config['sn_gan_data_path']
    results_path = config['results_path']

    global logging
    logging.basicConfig(filename=os.path.join(results_path, 'training.log'), level=logging.DEBUG)

    n_classes = dataset['n_classes'] if conditional else 0
    train_iter = dataset['train_iter']

    device = torch.device('cuda:{}'.format(config['gpu']) if torch.cuda.is_available() else 'cpu')
    logging.info("Using device {}\n".format(str(device)))
    d.to(device)
    g.to(device)

    logging.info("Starting training\n")
    print("Training")

    gen_losses = []
    dis_losses = []

    for iters in tqdm(range(max_iters)):
        for i in range(dis_iters):
            # train generator
            if i == 0:
                for p in d.parameters():
                    p.requires_grad = False

                g_optim.zero_grad()
                z = sample_z(noise_batch_size).to(device)
                y_fake = sample_c(noise_batch_size, n_classes)
                if y_fake is not None:
                    y_fake = y_fake.to(device)

                x_fake = g(z, y_fake)
                dis_fake = d(x_fake, y_fake)

                gen_loss = get_gen_loss(dis_fake)

                gen_loss.backward()
                g_optim.step()

                for p in d.parameters():
                    p.requires_grad = True

            # train discriminator
            ## compute probabilities
            x_real, y_real = next(train_iter)
            x_real = x_real.to(device)
            # y_real = y_real.to(device)

            if not conditional:
                y_real = None

            data_batch_size = x_real.shape[0]

            d_optim.zero_grad()
            dis_real = d(x_real, y_real)

            z = sample_z(data_batch_size).to(device)
            y_fake = sample_c(data_batch_size, n_classes)
            if y_fake is not None:
                y_fake = y_fake.to(device)
            x_fake = g(z, y_fake).detach()
            dis_fake = d(x_fake, y_fake)

            dis_loss = get_dis_loss(dis_fake, dis_real)
            if config['reparametrize']:
                dis_loss += lam1 * d.sum_gammas()**2
            if config['use_gp']:
                eps = torch.rand(data_batch_size, 1, 1, 1).to(device)
                # eps_c = torch.randint(0, 2, size=(data_batch_size,)).to(device)
                x_mid = eps * x_real + (1 - eps) * x_fake
                # y_mid = eps_c * y_real + (1 - eps_c) * y_fake   # this is kind of a hack but how else do you do a GP for a conditional discriminator???

                x_mid.detach()
                x_mid.requires_grad = True
                dis_mid = d(x_mid)
                grad_outputs = torch.ones_like(dis_mid).to(device)
                grads = torch.autograd.grad(
                    outputs=dis_mid, 
                    inputs=x_mid,
                    grad_outputs=grad_outputs,
                    create_graph=True, 
                    retain_graph=True, 
                    only_inputs=True
                )[0]
                gradient_penalty = ((torch.sum(grads**2, dim=(1,2,3))**.5 - 1)**2).mean(0)
                dis_loss += lam2 * gradient_penalty

            dis_loss.backward()
            if (iters) % 50 == 0 and i == 0:
                monitor_grad_singular_values(d)

                sv_dump_filename = os.path.join(results_path, 'sv_dump.json')
                json.dump(SV_DICT, open(sv_dump_filename, 'w'))

            d_optim.step()

        gen_losses += [gen_loss.cpu().data.numpy()]
        dis_losses += [dis_loss.cpu().data.numpy()]
    
        d_scheduler.step()
        g_scheduler.step()
        
        if (iters % 5000) == 0 and iters != 0:
            n_imgs = config['n_is_imgs']
            splits = 1
            if iters == 50000:
                n_imgs *= 10
                splits *= 10

            logging.info("Mean generator loss: {}".format(np.mean(gen_losses)))
            logging.info("Mean discriminator loss: {}".format(np.mean(dis_losses)))
            gen_losses = []
            dis_losses = []

            images = generate_images(g, n_imgs, config['eval_batch_size'])
            mean, std = calc_inception_chainer(images, splits=splits)
            logging.info("Inception Score: {}+/-{}".format(mean, std))
            print("Inception Score: {}+/-{}".format(mean, std))

            save_checkpoint(trainingwrapper)