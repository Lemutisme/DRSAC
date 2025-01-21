from utils import build_net, str2bool, evaluate_policy

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal, Categorical
import copy
from datetime import datetime
import gymnasium as gym
from gymnasium.wrappers import TransformReward
import os, shutil
import argparse
import math

######################################################
## TODO: Add the following imlementation
# 1. Add the main function to introduce the distribution shift
#    e.g. ai_safety_gym.environments.distributional_shift.py
#
# 2. Implement the Bellman operator of DRSAC
#    e.g. parts in SAC.SAC_continous and utils.Actor
######################################################

class Actor(nn.Module):
    def __init__(self, state_dim, action_dim, hid_shape, hidden_activation=nn.ReLU, output_activation=nn.ReLU):
        super(Actor, self).__init__()
        layers = [state_dim] + list(hid_shape) * 5

        self.a_net = build_net(layers, hidden_activation, output_activation)
        self.mu_layer = nn.Linear(layers[-1], action_dim)
        self.log_std_layer = nn.Linear(layers[-1], action_dim)

        self.LOG_STD_MAX = 2
        self.LOG_STD_MIN = -20

    def forward(self, state, deterministic, with_logprob):
        '''Network with Enforcing Action Bounds'''
        net_out = self.a_net(state)
        mu = self.mu_layer(net_out)
        log_std = self.log_std_layer(net_out)
        log_std = torch.clamp(log_std, self.LOG_STD_MIN, self.LOG_STD_MAX)  #总感觉这里clamp不利于学习
        # we learn log_std rather than std, so that exp(log_std) is always > 0
        std = torch.exp(log_std)
        dist = Normal(mu, std)
        u = mu if deterministic else dist.rsample()

        '''↓↓↓ Enforcing Action Bounds, see Page 16 of https://arxiv.org/pdf/1812.05905.pdf ↓↓↓'''
        a = torch.tanh(u)
        if with_logprob:
            # Get probability density of logp_pi_a from probability density of u:
            # logp_pi_a = (dist.log_prob(u) - torch.log(1 - a.pow(2) + 1e-6)).sum(dim=1, keepdim=True)
            # Derive from the above equation. No a, thus no tanh(h), thus less gradient vanish and more stable.
            logp_pi_a = dist.log_prob(u).sum(axis=1, keepdim=True) - (2 * (np.log(2) - u - F.softplus(-2 * u))).sum(axis=1, keepdim=True)
        else:
            logp_pi_a = None

        return a, logp_pi_a

class Double_Q_Critic(nn.Module):
    def __init__(self, state_dim, action_dim, hid_shape):
        super(Double_Q_Critic, self).__init__()
        layers = [state_dim + action_dim] + list(hid_shape) * 5

        self.Q_1 = build_net(layers, nn.ReLU, nn.Identity)
        self.Q_2 = build_net(layers, nn.ReLU, nn.Identity)   

    def forward(self, state, action):
        sa = torch.cat([state, action], 1)
        q1 = self.Q_1(sa)
        q2 = self.Q_2(sa)            
        return q1, q2
           
class Reward(nn.Module):
    def __init__(self, state_dim, action_dim, hid_shape, rtype, r_dim):
        super(Reward, self).__init__()
        layers = [state_dim + action_dim] + list(hid_shape) * 5
        self.rnet = build_net(layers, nn.ReLU, nn.Identity)
        self.rtype = rtype
        if self.rtype == 'continuous':
            self.mu_layer = nn.Linear(layers[-1], 1)
            self.log_std_layer = nn.Linear(layers[-1], 1)
        else:
            self.r_layer = nn.Linear(layers[-1], r_dim)
    
    def forward(self, state, action):
        sa = torch.cat([state, action], 1)
        r_out = self.rnet(sa)
        if self.rtype == 'continuous':
            mu = self.mu_layer(r_out)
            log_std = self.log_std_layer(r_out)
            #log_std = torch.clamp(log_std, 2, -20)  #总感觉这里clamp不利于学习
            std = torch.exp(log_std)
            dist = Normal(mu, std)
            r = dist.rsample()
        else:
            logits = self.r_layer[r_out]
            probs = F.softmax(logits, dim=1)
            # if deterministic:
            #     r = probs.argmax(-1).item()
            # else:
            #     r = Categorical(probs).sample().item()       
        return r
    
    def sample(self, state, action, num):
        if self.rtype == 'continuous':
            sa = torch.cat([state, action], 1)
            r_out = self.rnet(sa) 
            mu = self.mu_layer(r_out)
            log_std = self.log_std_layer(r_out)
            #log_std = torch.clamp(log_std, 2, -20)  #总感觉这里clamp不利于学习
            std = torch.exp(log_std)
            dist = Normal(mu, std)
            r = dist.rsample(sample_shape=(num,)) #shape = (num, batch_size, 1)
            r = r.permute(1, 0, 2).squeeze(-1) #shape = (batch_size, num)
        return r
            
class ReplayBuffer(object):
    def __init__(self, state_dim, action_dim, max_size, device):
        self.max_size = max_size
        self.device = device
        self.ptr = 0
        self.size = 0

        self.s = torch.zeros((max_size, state_dim) ,dtype=torch.float,device=self.device)
        self.a = torch.zeros((max_size, action_dim) ,dtype=torch.float,device=self.device)
        self.r = torch.zeros((max_size, 1) ,dtype=torch.float,device=self.device)
        self.s_next = torch.zeros((max_size, state_dim) ,dtype=torch.float,device=self.device)
        self.dw = torch.zeros((max_size, 1) ,dtype=torch.bool,device=self.device)

    def add(self, s, a, r, s_next, dw):
        #每次只放入一个时刻的数据
        self.s[self.ptr] = torch.from_numpy(s).to(self.device)
        self.a[self.ptr] = torch.from_numpy(a).to(self.device) # Note that a is numpy.array
        self.r[self.ptr] = r
        self.s_next[self.ptr] = torch.from_numpy(s_next).to(self.device)
        self.dw[self.ptr] = dw

        self.ptr = (self.ptr + 1) % self.max_size #存满了又重头开始存
        self.size = min(self.size + 1, self.max_size)

    def sample(self, batch_size):
        ind = torch.randint(0, self.size, device=self.device, size=(batch_size,))
        return self.s[ind], self.a[ind], self.r[ind], self.s_next[ind], self.dw[ind]

#reward engineering for better training
def Reward_adapter(r, EnvIdex):
    # For Pendulum-v0
    if EnvIdex == 0:
        r = (r + 8) / 8
    # For LunarLander
    elif EnvIdex == 1:
        if r <= -100: r = -10
    # For BipedalWalker
    elif EnvIdex == 4 or EnvIdex == 5:
        if r <= -100: r = -1
    return r

def Action_adapter(a,max_action):
    #from [-1,1] to [-max,max]
    return  a*max_action

def Action_adapter_reverse(act,max_action):
    #from [-max,max] to [-1,1]
    return  act/max_action

class NoiseReward(gym.RewardWrapper):
    def __init__(self, env, func):
        super().__init__(env)
        self.env = env
        self._max_episode_steps = env._max_episode_steps
        self.func = func

    def step(self, action):
        obs, reward, dw, tr, info = self.env.step(action)
        modified_reward = self.func(reward)
        return obs, modified_reward, dw, tr, info


class SAC_countinuous():
    def __init__(self, **kwargs):
        # Init hyperparameters for agent, just like "self.gamma = opt.gamma, self.lambd = opt.lambd, ..."
        self.__dict__.update(kwargs)
        if self.robust:
            print('This is a robust policy.\n')
            train_var = self.train_std ** 2
            eval_var = self.eval_std ** 2
            self.delta = 0.5*(eval_var / train_var + math.log(train_var / eval_var)- 1)
            assert self.delta >= 0
        self.tau = 0.005
        # if self.train_noise:
        #     self.delta = 0.5 * math.log(2 * math.pi * (self.train_std **2))
        # if self.eval_noise:
        #     self.delta = 0.5 * math.log(2 * math.pi * (self.eval_std **2))

        self.actor = Actor(self.state_dim, self.action_dim, (self.net_width,self.net_width)).to(self.device)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=self.a_lr)

        self.q_critic = Double_Q_Critic(self.state_dim, self.action_dim, (self.net_width,self.net_width)).to(self.device)
        self.q_critic_optimizer = torch.optim.Adam(self.q_critic.parameters(), lr=self.c_lr)
        self.q_critic_target = copy.deepcopy(self.q_critic)
        # Freeze target networks with respect to optimizers (only update via polyak averaging)
        for p in self.q_critic_target.parameters():
            p.requires_grad = False
        
        if self.robust:    
            self.reward = Reward(self.state_dim, self.action_dim, (self.net_width,self.net_width), self.rtype, self.r_dim).to(self.device)
            self.reward_optimizer = torch.optim.Adam(self.reward.parameters(), lr=self.r_lr)
            
            self.beta = torch.zeros((self.batch_size, 1), requires_grad=True, device=self.device)
            self.beta_optimizer = torch.optim.Adam([self.beta], lr=self.b_lr)

        self.replay_buffer = ReplayBuffer(self.state_dim, self.action_dim, max_size=int(1e6), device=self.device)

        if self.adaptive_alpha:
            # Target Entropy = −dim(A) (e.g. , -6 for HalfCheetah-v2) as given in the paper
            self.target_entropy = torch.tensor(-self.action_dim, dtype=float, requires_grad=True, device=self.device)
            # We learn log_alpha instead of alpha to ensure alpha>0
            self.log_alpha = torch.tensor(np.log(self.alpha), dtype=float, requires_grad=True, device=self.device)
            self.alpha_optim = torch.optim.Adam([self.log_alpha], lr=self.c_lr)

    def select_action(self, state, deterministic):
        # only used when interact with the env
        with torch.no_grad():
            state = torch.FloatTensor(state[np.newaxis,:]).to(self.device)
            a, _ = self.actor(state, deterministic, with_logprob=False)
        return a.cpu().numpy()[0]

    def train(self,):
        s, a, r, s_next, dw = self.replay_buffer.sample(self.batch_size)
        #----------------------------- ↓↓↓↓↓ Update R Net ↓↓↓↓↓ ------------------------------#
        if self.robust:
            r_pred = self.reward(s, a)
            r_loss = F.mse_loss(r_pred, r)
            self.reward_optimizer.zero_grad()
            r_loss.backward()
            self.reward_optimizer.step()
            
            with torch.no_grad():
                r_sample = self.reward.sample(s, a, 50)
            
            def dual_func(r, beta):
                size = r_sample.shape[1]
                return - beta * (torch.logsumexp(-r/beta, dim=1, keepdim=True) - math.log(size)) - beta * self.delta
        
            for _ in range(10):
                self.exp_beta = torch.exp(self.beta)
                opt_loss = -dual_func(r_sample, self.exp_beta)
                self.beta_optimizer.zero_grad()
                opt_loss.sum().backward()
                self.beta_optimizer.step() 
            
            r_opt = dual_func(r_sample, torch.exp(self.beta)) 

        #----------------------------- ↓↓↓↓↓ Update Q Net ↓↓↓↓↓ ------------------------------#
        with torch.no_grad():
            a_next, log_pi_a_next = self.actor(s_next, deterministic=False, with_logprob=True)
            target_Q1, target_Q2 = self.q_critic_target(s_next, a_next)
            target_Q = torch.min(target_Q1, target_Q2)
            #############################################################		
            ### r + γ * (1 - done) * E_pi(Q(s',a') - α * logπ(a'|s')) ###
            if self.robust:
                target_Q = r_opt + (~dw) * self.gamma * (target_Q - self.alpha * log_pi_a_next)
            else:
                target_Q = r + (~dw) * self.gamma * (target_Q - self.alpha * log_pi_a_next) 
            #############################################################
                
        # Get current Q estimates
        current_Q1, current_Q2 = self.q_critic(s, a)

        # JQ(θ)
        q_loss = F.mse_loss(current_Q1, target_Q) + F.mse_loss(current_Q2, target_Q) 
        self.q_critic_optimizer.zero_grad()
        q_loss.backward()
        self.q_critic_optimizer.step()

        #----------------------------- ↓↓↓↓↓ Update Actor Net ↓↓↓↓↓ ------------------------------#
        # Freeze critic so you don't waste computational effort computing gradients for them when update actor
        for params in self.q_critic.parameters():
            params.requires_grad = False

        a, log_pi_a = self.actor(s, deterministic=False, with_logprob=True)
        current_Q1, current_Q2 = self.q_critic(s, a)
        Q = torch.min(current_Q1, current_Q2)

        # Entropy Regularization
        # Note that the entropy term is not included in the loss function
        #########################################
          ### Jπ(θ) = E[α * logπ(a|s) - Q(s,a)] ###
        a_loss = (self.alpha * log_pi_a - Q).mean()
        #########################################
        self.actor_optimizer.zero_grad()
        a_loss.backward()
        self.actor_optimizer.step()
        
        for params in self.q_critic.parameters():
            params.requires_grad = True

        #----------------------------- ↓↓↓↓↓ Update alpha ↓↓↓↓↓ ------------------------------#
        if self.adaptive_alpha: # Adaptive alpha SAC
            # We learn log_alpha instead of alpha to ensure alpha>0
            alpha_loss = -(self.log_alpha * (log_pi_a + self.target_entropy).detach()).mean()
            self.alpha_optim.zero_grad()
            alpha_loss.backward()
            self.alpha_optim.step()
            self.alpha = self.log_alpha.exp()

        #----------------------------- ↓↓↓↓↓ Update Target Net ↓↓↓↓↓ ------------------------------#
        for param, target_param in zip(self.q_critic.parameters(), self.q_critic_target.parameters()):
            target_param.data.copy_(self.tau * param.data + (1 - self.tau) * target_param.data)

    def save(self,EnvName, timestep):
        torch.save(self.actor.state_dict(), "./model/{}_actor{}.pth".format(EnvName,timestep))
        torch.save(self.q_critic.state_dict(), "./model/{}_q_critic{}.pth".format(EnvName,timestep))

    def load(self,EnvName, timestep):
        self.actor.load_state_dict(torch.load("./model/{}_actor{}.pth".format(EnvName, timestep), map_location=self.device))
        self.q_critic.load_state_dict(torch.load("./model/{}_q_critic{}.pth".format(EnvName, timestep), map_location=self.device))

def main(opt):
    """
    Main function to train and evaluate an SAC agent on different environments.
    """

    # 1. Define environment names and abbreviations
    EnvName = [
        'Pendulum-v1',
        'LunarLanderContinuous-v3',
        'Humanoid-v5',
        'HalfCheetah-v4',
        'BipedalWalker-v3',
        'BipedalWalkerHardcore-v3',
        'FrozenLake-v1'
    ]
    BrifEnvName = [
        'PV1',
        'LLdV2',
        'Humanv5',
        'HCv4',
        'BWv3',
        'BWHv3',
        'CRv3'
    ]
    EnvR = [
        0,
        0,
        0,
        0,
        2,
        0,
        3
    ]

    # 2. Create training and evaluation environments
    env = gym.make(
        EnvName[opt.EnvIdex],
        render_mode="human" if opt.render else None
    )
    if opt.train_noise:
        train_dist = Normal(0, opt.train_std)
        env = NoiseReward(env, lambda r: r + train_dist.sample())

    eval_env = gym.make(EnvName[opt.EnvIdex])
    if opt.eval_noise:
        eval_dist = Normal(0, opt.eval_std)
        eval_env = TransformReward(eval_env, lambda r: r + eval_dist.sample())

    # 3. Extract environment properties
    opt.state_dim = env.observation_space.shape[0]
    opt.action_dim = env.action_space.shape[0]  # Continuous action dimension
    opt.max_action = float(env.action_space.high[0])  # Action range [-max_action, max_action]
    opt.max_e_steps = env._max_episode_steps
    if opt.EnvIdex in [0,1,2,3,5]:
        opt.rtype = 'continuous'
    else:
        opt.rtype = 'discrete'
    opt.r_dim = EnvR[opt.EnvIdex]
        

    # 4. Print environment info
    print(
        f"Env: {EnvName[opt.EnvIdex]}  "
        f"state_dim: {opt.state_dim}  "
        f"action_dim: {opt.action_dim}  "
        f"max_a: {opt.max_action}  "
        f"min_a: {env.action_space.low[0]}  "
        f"max_e_steps: {opt.max_e_steps}"
    )

    # 5. Seed everything for reproducibility
    env_seed = opt.seed
    torch.manual_seed(opt.seed)
    torch.cuda.manual_seed(opt.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    print(f"Random Seed: {opt.seed}")

    # 6. Set up TensorBoard for logging (if requested)
    writer = None
    if opt.write:
        from torch.utils.tensorboard import SummaryWriter
        # timenow = str(datetime.now())[:-10]    # e.g. 2025-01-10 17:45
        # timenow = ' ' + timenow[:13] + '_' + timenow[-2:]  # e.g. ' 2025-01-10_45'
        writepath = f"runs/{BrifEnvName[opt.EnvIdex]}"
        if opt.train_noise:
            writepath += f"/Train Noise {opt.train_std}"
        if opt.eval_noise:
            writepath += f"/Eval Noise {opt.eval_std}"
        if opt.robust:
             writepath += f"/Robust"
        if os.path.exists(writepath):
            shutil.rmtree(writepath)
        writer = SummaryWriter(log_dir=writepath)

    # 7. Create a directory for saving models
    if not os.path.exists('model'):
        os.mkdir('model')

    # 8. Initialize the SAC agent
    agent = SAC_countinuous(**vars(opt))  # Convert argparse Namespace to dict

    # 9. Load a saved model if requested
    if opt.Loadmodel:
        agent.load(BrifEnvName[opt.EnvIdex], opt.ModelIdex)

    # 10. If rendering mode is on, run an infinite evaluation loop
    if opt.render:
        while True:
            score = evaluate_policy(env, agent, turns=1)
            print(f"EnvName: {BrifEnvName[opt.EnvIdex]}, Score: {score}")

    # 11. Otherwise, proceed with training
    else:
        total_steps = 0
        total_episode = 0

        while total_steps < opt.Max_train_steps:
            # (a) Reset environment with incremented seed
            state, info = env.reset(seed=env_seed)
            env_seed += 1
            total_episode += 1
            done = False

            # (b) Interact with environment until episode finishes
            while not done:
                # Random exploration for first 5 episodes (each episode is up to max_e_steps)
                if total_steps < (10 * opt.max_e_steps):
                    # Sample action directly from environment's action space
                    action_env = env.action_space.sample()  # Range: [-max_action, max_action]
                    # Convert env action back to agent's internal range [-1,1]
                    action_agent = Action_adapter_reverse(action_env, opt.max_action)
                else:
                    # Select action from agent (internal range [-1,1])
                    action_agent = agent.select_action(state, deterministic=False)
                    # Convert agent action to environment range
                    action_env = Action_adapter(action_agent, opt.max_action)

                # Step the environment
                next_state, reward, dw, tr, info = env.step(action_env)

                # Custom reward shaping, if needed
                reward = Reward_adapter(reward, opt.EnvIdex)

                # Check for terminal state
                done = (dw or tr)

                # Store transition in replay buffer
                agent.replay_buffer.add(state, action_agent, reward, next_state, dw)

                # Move to next step
                state = next_state
                total_steps += 1

                # (c) Train the agent at fixed intervals (batch updates)
                if (total_steps >= 10 * opt.max_e_steps) and (total_steps % opt.update_every == 0):
                    for _ in range(opt.update_every):
                        agent.train()
                    agent.a_lr *= 0.9
                    agent.c_lr *= 0.9

                # (d) Evaluate and log periodically
                # if total_steps % opt.eval_interval == 0:
                #     ep_r = evaluate_policy(eval_env, agent, turns=3)
                #     if writer is not None:
                #         writer.add_scalar('ep_r', ep_r, global_step=total_steps)
                #     print(
                #         f"EnvName: {BrifEnvName[opt.EnvIdex]}, "
                #         f"Steps: {int(total_steps/1000)}k, "
                #         f"Episodes: {total_episode}, "
                #         f"Episode Reward: {ep_r}"
                #     )
                if total_steps % opt.eval_interval == 0:
                    print(f"Steps: {int(total_steps/1000)}k")

                # (e) Save model at fixed intervals
                # if total_steps % opt.save_interval == 0:
                #     agent.save(BrifEnvName[opt.EnvIdex], int(total_steps / 1000))
        
        # 11.5 Compute score of 20 episode
        eval_num = 20
        print(f"Train finished. Now generate scores of {eval_num} episodes.")
        scores = []
        for _ in range(eval_num):
            score = evaluate_policy(env, agent, turns=1)
            scores.append(score)

        # 12. Close environments after training
        env.close()
        eval_env.close()

        return scores

if __name__ == '__main__':
    '''Hyperparameter Setting'''
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', type=str, default='cuda', help='running device: cuda or cpu')
    parser.add_argument('--EnvIdex', type=int, default=0, help='PV1, Lch_Cv2, Humanv4, HCv4, BWv3, BWHv3, CRv3')
    parser.add_argument('--write', type=str2bool, default=False, help='Use SummaryWriter to record the training')
    parser.add_argument('--render', type=str2bool, default=False, help='Render or Not')
    parser.add_argument('--Loadmodel', type=str2bool, default=False, help='Load pretrained model or Not')
    parser.add_argument('--ModelIdex', type=int, default=100, help='which model to load')

    parser.add_argument('--seed', type=int, default=0, help='random seed')
    parser.add_argument('--Max_train_steps', type=int, default=int(1e5), help='Max training steps')
    parser.add_argument('--save_interval', type=int, default=int(100e3), help='Model saving interval, in steps.')
    parser.add_argument('--eval_interval', type=int, default=int(2.5e3), help='Model evaluating interval, in steps.')
    parser.add_argument('--update_every', type=int, default=50, help='Training Fraquency, in stpes')

    parser.add_argument('--gamma', type=float, default=0.99, help='Discounted Factor')
    parser.add_argument('--net_width', type=int, default=256, help='Hidden net width, s_dim-400-300-a_dim')
    parser.add_argument('--a_lr', type=float, default=3e-5, help='Learning rate of actor')
    parser.add_argument('--c_lr', type=float, default=3e-5, help='Learning rate of critic')
    parser.add_argument('--b_lr', type=float, default=3e-5, help='Learning rate of dual-form optimization')
    parser.add_argument('--r_lr', type=float, default=3e-5, help='Learning rate of reward net')
    parser.add_argument('--batch_size', type=int, default=256, help='batch_size of training')
    parser.add_argument('--alpha', type=float, default=0.12, help='Entropy coefficient')
    parser.add_argument('--adaptive_alpha', type=str2bool, default=True, help='Use adaptive_alpha or Not')
    
    parser.add_argument('--robust', type=bool, default=False, help='Robust policy')
    parser.add_argument('--train_noise', type=bool, default=False, help='Train Env is Noisy')
    parser.add_argument('--train_std', type=float, default=1.0, help='Standard Deviation of Train Env Reward')
    parser.add_argument('--eval_noise', type=bool, default=False, help='Evaluation Env is Noisy')
    parser.add_argument('--eval_std', type=float, default=1.0, help='Standard Deviation of Eval Env Reward')
    opt = parser.parse_args()
    opt.device = torch.device(opt.device) # from str to torch.device
    opt.train_noise = True
    opt.eval_noise = True
    opt.eval_std = 0.1
    opt.train_std = 0.2

    scores = []
    for _ in range(3):
        opt.train_std += 0.1
        
        print("---------------")
        print(opt)

        scores.append([opt.train_std, opt.eval_std] + main(opt))
    
    filename = "robust.txt" if opt.robust else "non-robust.txt"
    with open(filename, 'a') as f:
        for score in scores:
            f.write(f"{score}\n")

