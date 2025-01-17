import gym
import enum
import time
import acme
import torch
import dm_env
import random
import warnings
import itertools
import collections

import numpy as np
import pandas as pd
import torch.nn as nn
import matplotlib.pyplot as plt
from torch.nn.functional import mse_loss

from acme import specs
from acme import wrappers
from acme.utils import tree_utils
import copy

from auxrl.networks.IQNNetwork import Network
from auxrl.ReplayBuffer import ReplayBuffer

Transitions = collections.namedtuple(
    'Transitions', ['state', 'action', 'reward', 'discount', 'next_state'])

class Agent(acme.Actor):
    """
    Agent where the model-free component is an implicit quantile network.
    This class does not have all the options of the Actor class (long horizon
    prediction, POMDP), but it does support one-timestep prediction as an
    auxiliary task to the base IQN agent.
    """

    def __init__(self,
        env_spec: specs.EnvironmentSpec, network: Network,
        loss_weights: list=[0,0,0,1], lr: float=1e-4, epsilon: float=1.,
        replay_capacity: int=1_000_000,
        batch_size: int=32, target_update_frequency: int=1000,
        device: torch.device=torch.device('cpu'), discount_factor: float=0.9):

        self._env_spec = env_spec
        self._loss_weights = loss_weights
        self._n_actions = env_spec.actions.num_values
        # Initialize networks
        self._network = network
        self._target_network = network.copy()
        self._replay_buffer = ReplayBuffer(replay_capacity)
        # Store training parameters
        self._epsilon = epsilon
        self._batch_size = batch_size
        self._lr = lr
        self._target_update_frequency = target_update_frequency
        self._device = device
        self._discount_factor = discount_factor
        # Initialize optimizer
        self._optimizer = torch.optim.Adam(
            self._network.get_trainable_params(), lr=lr)
        self._n_updates = 0

    def reset(self):
        self._network.encoder.reset()
        self._target_network.encoder.reset()

    def select_action(
        self, obs, force_greedy=False, verbose=False, return_latent=False
        ):
        """ Epsilon-greedy action selection. """

        with torch.no_grad():
            z = self._network.encoder(
                torch.tensor(obs).unsqueeze(0).to(self._device))
            quantile_vals, _ = self._network.Q(z)
        quantile_vals = quantile_vals.squeeze(0).detach()
        mean_quantile_vals = quantile_vals.mean(0)
        if force_greedy or (self._epsilon < torch.rand(1)):
            if verbose: print(quantile_vals)
            action = mean_quantile_vals.argmax(axis=-1)
        else:
            action = torch.randint(
                low=0, high=self._n_actions , size=(1,), dtype=torch.int64)
        if return_latent:
            return action, z
        else:
            return action

    def get_curr_latent(self):
        return self._network.encoder.get_curr_latent()

    def get_huber_loss(self, td_errors, k):
        """
        Calculate huber loss element-wisely depending on kappa k.
        """
        loss = torch.where(
            td_errors.abs() <= k, 0.5 * td_errors.pow(2), k * (td_errors.abs() - 0.5 * k))
        return loss

    def update(self, clip_norm=-1):
        batch_size = self._batch_size
        if not self._replay_buffer.is_ready(batch_size, 1):
            return [0,0,0,0,0]
        device = self._device
        self._optimizer.zero_grad()
        transitions_seq = self._replay_buffer.sample(batch_size)
        transitions = transitions_seq

        # Unpack transition information
        obs = torch.tensor(transitions.obs.astype(np.float32)).to(device) # (N,C,H,W)
        a = transitions.action.astype(int) # (N,1)
        r = torch.tensor(
            transitions.reward.astype(np.float32)).view(-1,1).to(device) # (N,1)
        terminal = torch.tensor(
            transitions.terminal.astype(np.float32)).view(-1,1).to(device) # (N,1)
        next_obs = torch.tensor(
            transitions.next_obs.astype(np.float32)).to(device)
        onehot_actions = np.zeros((batch_size, self._n_actions))
        onehot_actions[np.arange(batch_size), a.squeeze()] = 1
        onehot_actions = torch.as_tensor(onehot_actions, device=self._device).float()

        # Run through encoder
        next_z = self._network.encoder(next_obs)
        z = self._network.encoder(obs)
        z_and_action = torch.cat([z, onehot_actions], dim=1)
        Tz = self._network.T(z_and_action)

        # Positive Sample Loss (transition predictions)
        T_target = next_z
        loss_pos_sample = torch.nn.functional.mse_loss(
            Tz, T_target, reduction='none')
        loss_pos_sample = torch.mean(loss_pos_sample)

        # Negative Sample Loss (entropy)
        rolled = torch.roll(z, 1, dims=0)
        entropy_temp = 5
        loss_neg_random = torch.mean(torch.exp(
            -entropy_temp * torch.norm(z - rolled, dim=1)))
        loss_neg_neighbor = torch.mean(torch.exp(
            -entropy_temp * torch.norm(z - next_z, dim=1)))

        # Update target network if needed
        if self._n_updates%self._target_update_frequency == 0:
            self._target_network.set_params(self._network.get_params())

        # IQN update
        with torch.no_grad():
            target_next_q, quantiles = self._target_network.Q(next_z) # (N, Q, a)
            next_action = torch.argmax(target_next_q.mean(1), axis=1) # (N)
            max_next_q = target_next_q[
                np.arange(batch_size), :, next_action] # (N, Q)
            rewards = r.repeat(1, quantiles.shape[1])
            done = (1. - terminal).repeat(1, quantiles.shape[1])
            target_q_vals = rewards + self._discount_factor*max_next_q*done
        current_q_vals, quantiles = self._network.Q(z)
        current_q_vals = current_q_vals[np.arange(batch_size), :, a.squeeze()]
        td_error = target_q_vals.unsqueeze(1) - current_q_vals.unsqueeze(2)
        assert td_error.shape == (td_error.shape[0], 8, 8), "Wrong shape"
        kappa = 1.0
        huber_loss = self.get_huber_loss(td_error, kappa)
        quantile_loss = abs(quantiles - (td_error.detach()<0).float()) * huber_loss / kappa
        quantile_loss = quantile_loss.sum(dim=1).mean(dim=1)
        loss_Q = torch.mean(quantile_loss)

        # Aggregate all losses and update parameters
        all_losses = self._loss_weights[0] * loss_pos_sample \
            + self._loss_weights[1] * loss_neg_neighbor \
            + self._loss_weights[2] * loss_neg_random \
            + self._loss_weights[3] * loss_Q
        all_losses.backward()
        if clip_norm != -1:
            nn.utils.clip_grad_norm_(self._network.get_encoder_params(), clip_norm)
        self._optimizer.step()
        self._n_updates += 1

        # Update target network if needed
        if self._n_updates % self._target_update_frequency == 0:
            print(f'Q loss at step {self._n_updates}: {loss_Q.item()}')

        return [
            self._loss_weights[0]*loss_pos_sample.item(),
            self._loss_weights[1]*loss_neg_neighbor.item(),
            self._loss_weights[2]*loss_neg_random.item(),
            self._loss_weights[3]*loss_Q.item(), all_losses.item()]

    def observe_first(self, timestep: dm_env.TimeStep):
        self._replay_buffer.add_first(timestep)

    def observe(
        self, action: int, next_timestep: dm_env.TimeStep,
        latent: torch.tensor):
        """
        If NEXT_TIMESTEP is at time t+1, then LATENT corresponds to the
        observation from time t.
        """

        self._replay_buffer.add(action, next_timestep, latent)

    def save_network(self, path, episode=None):
        network_params = self._network.get_params()
        file_suffix = '' if episode == None else f'_ep{episode}'
        torch.save(network_params, f'{path}network{file_suffix}.pth')

    def load_network(
        self, path, episode=None, encoder_only=True, shuffle=False):
        file_suffix = '' if episode == None else f'_ep{episode}'
        try:
            network_params = torch.load(f'{path}network{file_suffix}.pth')
        except:
            network_params = torch.load(
                f'{path}network{file_suffix}.pth', map_location=torch.device('cpu')
                )
        self._network.set_params(network_params, encoder_only, shuffle=shuffle)

