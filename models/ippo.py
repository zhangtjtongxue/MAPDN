import torch
import torch.nn as nn
import numpy as np
from utilities.util import select_action, cuda_wrapper, batchnorm
from models.model import Model
from learning_algorithms.ppo import PPO
from collections import namedtuple
from critics.mlp_critic_ppo import MLPCritic
from agents.mlp_agent_ppo import MLPAgent


class IPPO(Model):
    def __init__(self, args, target_net=None):
        super(IPPO, self).__init__(args)
        self.construct_model()
        self.apply(self.init_weights)
        if target_net != None:
            self.target_net = target_net
            self.reload_params_to_target()
        self.rl = PPO(self.args)

    def construct_value_net(self):
        input_shape = self.obs_dim + self.n_ # it is a v(s) rather than q(s, a)
        # input_shape = self.obs_dim # it is a v(s) rather than q(s, a)
        output_shape = 1
        self.value_dicts = nn.ModuleList( [ MLPCritic(input_shape, output_shape, self.args) ] )

    def construct_policy_net(self):
        self.policy_dicts = nn.ModuleList([ MLPAgent(self.obs_dim+self.n_, self.args) ])
        # self.policy_dicts = nn.ModuleList([ MLPAgent(self.obs_dim, self.args) ])
        
    def construct_model(self):
        self.construct_value_net()
        self.construct_policy_net()

    def value(self, obs, act):
        # obs_shape = (b, n, o)
        # act_shape = (b, n, a)
        batch_size = obs.size(0)

        # add agent id
        agent_ids = torch.eye(self.n_).unsqueeze(0).repeat(batch_size, 1, 1) # shape = (b, n, n)
        agent_ids = cuda_wrapper(agent_ids, self.cuda_)
        obs = torch.cat( (obs, agent_ids), dim=-1 ) # shape = (b, n, o+n)

        obs = obs.contiguous().view(batch_size*self.n_, -1)

        agent_value = self.value_dicts[0]
        inputs = obs
        values, _ = agent_value(inputs, None)
        values = values.contiguous().view(batch_size, self.n_, -1)

        return values

    def get_actions(self, state, status, exploration, actions_avail, target=False):
        if self.args.continuous:
            means, log_stds, _ = self.policy(state) if not target else self.target_net.policy(state)
            if means.size(-1) > 1:
                means_ = means.sum(dim=1, keepdim=True)
                log_stds_ = log_stds.sum(dim=1, keepdim=True)
            else:
                means_ = means
                log_stds_ = log_stds
            actions, log_prob_a = select_action(self.args, means_, status=status, exploration=exploration, info={'log_std': log_stds_})
            restore_mask = 1. - cuda_wrapper((actions_avail == 0).float(), self.cuda_)
            restore_actions = restore_mask * actions
            action_out = (means, log_stds)
        else:
            logits, _, _ = self.policy(state) if not target else self.target_net.policy(state)
            logits[actions_avail == 0] = -9999999
            actions, log_prob_a = select_action(self.args, logits, status=status, exploration=exploration)
            restore_actions = actions
            action_out = logits
        return actions, restore_actions, log_prob_a, action_out

    def get_loss(self, batch):
        policy_loss, value_loss, action_out = self.rl.get_loss(batch, self, self.target_net)
        return policy_loss, value_loss, action_out