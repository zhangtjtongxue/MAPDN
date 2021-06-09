import torch
import torch.nn as nn
import numpy as np
from utilities.util import select_action, cuda_wrapper, batchnorm, multinomials_log_density, normal_log_density
from models.model import Model
from collections import namedtuple
from critics.mlp_critic import MLPCritic


class COMA(Model):
    def __init__(self, args, target_net=None):
        super(COMA, self).__init__(args)
        self.construct_model()
        self.apply(self.init_weights)
        if target_net != None:
            self.target_net = target_net
            self.reload_params_to_target()

    def construct_value_net(self):
        if self.args.continuous:
            input_shape = (self.n_ + 1) * self.obs_dim + self.n_ * self.act_dim + self.n_
            # input_shape = (self.n_ + 1) * self.obs_dim + self.n_ * self.act_dim
            output_shape = 1
        else:
            input_shape = (self.n_ + 1) * self.obs_dim + self.n_ * self.act_dim + self.n_
            # input_shape = (self.n_ + 1) * self.obs_dim + self.n_ * self.act_dim
            output_shape = self.act_dim
        self.value_dicts = nn.ModuleList( [ MLPCritic(input_shape, output_shape, self.args) ] )

    def construct_model(self):
        self.construct_value_net()
        self.construct_policy_net()

    def value(self, obs, act):
        gaussian_flag = (obs.size(0) != act.size(0))
        if gaussian_flag:
            batch_size = obs.size(0)
            obs_own = obs.clone()
            obs_own = obs_own.unsqueeze(0).expand(self.sample_size, batch_size, self.n_, self.obs_dim).contiguous().view(-1, self.n_, self.obs_dim) # (s*b, n, o)
            obs = obs.unsqueeze(0).unsqueeze(2).expand(self.sample_size, batch_size, self.n_, self.n_, self.obs_dim) # shape = (b, n, o) -> (1, b, 1, n, o) -> (s, b, n, n, o)
            obs = obs.contiguous().view(-1, self.n_, self.n_*self.obs_dim) # (s*b, n, n*o)
            inp = torch.cat( (obs, obs_own), dim=-1 ) # shape = (s*b, n, o*n+o)
        else:
            batch_size = obs.size(0)
            obs_own = obs.clone()
            obs = obs.unsqueeze(1).expand(batch_size, self.n_, self.n_, self.obs_dim) # shape = (b, n, o) -> (b, 1, n, o) -> (b, n, n, o)
            obs = obs.contiguous().view(batch_size, self.n_, -1) # shape = (b, n, o*n)
            inp = torch.cat((obs, obs_own), dim=-1) # shape = (b, n, o*n+o)

        # add agent id
        agent_ids = torch.eye(self.n_).unsqueeze(0).repeat(inp.size(0), 1, 1) # shape = (b/s*b, n, n)
        agent_ids = cuda_wrapper(agent_ids, self.cuda_)
        inp = torch.cat( (inp, agent_ids), dim=-1 ) # shape = (b/s*b, n, o*n+o+n)

        inp = inp.contiguous().view( -1, self.obs_dim*(self.n_+1)+self.n_ ) # shape = (b/s*b, o*n+o+n)
        # inp = inp.contiguous().view( -1, self.obs_dim*(self.n_+1) ) # shape = (b/s*b, o*n+o)
        agent_value = self.value_dicts[0]

        if self.args.continuous:
            if gaussian_flag:
                act = act.contiguous().view(-1, self.n_*self.act_dim)
                inputs = torch.cat( (inp, act), dim=-1 )
            else:
                act_reshape = act.unsqueeze(1).repeat(1, self.n_, 1, 1) # shape = (b, n, n, a)
                act_reshape = act_reshape.contiguous().view(-1, self.n_*self.act_dim)
                inputs = torch.cat( (inp, act_reshape), dim=-1 )
        else:
            # other agents' actions
            act_repeat = act.unsuqeeze(1).repeat(1, self.n_, 1, 1) # shape = (b, n, n, a)
            agent_mask = torch.eye(self.n_).unsqueeze(0).unsqueeze(-1).expand_as(act_repeat) # shape = (b, n, n, a)
            agent_mask = cuda_wrapper(agent_mask, self.cuda_)
            agent_mask_complement = 1. - agent_mask
            act_mask_out = agent_mask_complement * act_repeat # shape = (b, n, n, a)
            act_other = act_mask_out.contiguous().view(-1, self.n_*self.act_dim) # shape = (b*n, n*a)
            inputs = torch.cat( (inp, act_other), dim=-1 )
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
        batch_size = len(batch.state)
        state, actions, old_log_prob_a, old_values, old_next_values, rewards, next_state, done, last_step, actions_avail = self.unpack_data(batch)
        if self.args.continuous:
            means, log_stds, _ = self.policy(state)
            action_out = (means, log_stds)
            log_prob_a = normal_log_density(actions, means, log_stds)
            _, next_actions, _, _ = self.get_actions(next_state, status='train', exploration=True, actions_avail=actions_avail, target=self.args.target)
            self.sample_size = self.args.sample_size
            means, log_stds = action_out
            means_repeat = means.unsqueeze(0).repeat(self.sample_size, 1, 1, 1) # (s,b,n,a)
            log_stds_repeat = log_stds.unsqueeze(0).repeat(self.sample_size, 1, 1, 1) # (s,b,n,a)
            _sampled_actions_repeat = torch.normal(means_repeat, log_stds_repeat.exp()) # (s,b,n,a)
            sampled_actions_repeat = _sampled_actions_repeat.unsqueeze(2).repeat(1, 1, self.n_, 1, 1) # (s,b,n,a) -> (s,b,n,n,a)
            actions_repeat = actions.unsqueeze(0).unsqueeze(2).repeat(self.sample_size, 1, self.n_, 1, 1) # (s,b,n,n,a)
            agent_mask = torch.eye(self.n_).unsqueeze(0).unsqueeze(1).unsqueeze(-1).expand_as(actions_repeat) # (s,b,n,n,a)
            agent_mask = cuda_wrapper(agent_mask, self.cuda_)
            agent_mask_complement = 1. - agent_mask
            actions_repeat_merge = actions_repeat * agent_mask_complement + sampled_actions_repeat * agent_mask # (s,b,n,n,a)
            actions_repeat_merge = actions_repeat_merge.contiguous().view(-1, self.n_, self.n_*self.act_dim) # (s*b,n,n*a)
            values_sampled = self.value(state, actions_repeat_merge).contiguous().view(self.sample_size, batch_size, self.n_) # (s*b,n,1) -> (s,b,n)
            baselines = torch.mean(values_sampled, dim=0) # (b,n)
            values = self.value(state, actions).squeeze(-1) # (b,n,a) -> (b,n) action value
        else:
            logits, _ = self.policy(state)
            action_out = logits
            log_prob_a = multinomials_log_density(actions, logits)
            _, next_actions, _, _ = self.get_actions(next_state, status='train', exploration=True, target=self.args.target)
            values = self.value(state, actions) # (b,n,a) action value
            baselines = torch.sum(values*torch.softmax(logits, dim=-1), dim=-1) # the only difference to ActorCritic is this  baseline (b,n)
            values = torch.sum(values*actions, dim=-1) # (b,n)
        if self.args.target:
            next_values = self.target_net.value(next_state, next_actions)
        else:
            next_values = self.value(next_state, next_actions)
        if self.args.continuous:
            next_values = next_values.squeeze(-1) # (b,n)
        else:
            next_values = torch.sum(next_values*next_actions, dim=-1) # (b,n)
        # calculate the advantages
        returns = cuda_wrapper(torch.zeros((batch_size, self.n_), dtype=torch.float), self.cuda_)
        assert values.size() == next_values.size()
        assert returns.size() == values.size()
        for i in reversed(range(rewards.size(0))):
            if last_step[i]:
                next_return = 0 if done[i] else next_values[i].detach()
            else:
                next_return = next_values[i].detach()
            returns[i] = rewards[i] + self.args.gamma * next_return
        # value loss
        deltas = returns - values
        # value_loss = deltas.pow(2).mean(dim=0)
        value_loss = deltas.pow(2).mean()
        # actio loss
        advantages = ( values - baselines ).detach()
        if self.args.normalize_advantages:
            advantages = batchnorm(advantages)
        restore_mask = 1. - cuda_wrapper((actions_avail == 0).float(), self.cuda_)
        log_prob_a = (restore_mask * log_prob_a).sum(dim=-1)
        log_prob_a = log_prob_a.squeeze(-1)
        assert log_prob_a.size() == advantages.size(), f"log_prob size is: {log_prob_a.size()} and advantages size is {advantages.size()}."
        policy_loss = - advantages * log_prob_a
        # policy_loss = policy_loss.mean(dim=0)
        policy_loss = policy_loss.mean()
        return policy_loss, value_loss, action_out