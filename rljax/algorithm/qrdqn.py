import os
from functools import partial
from typing import Any, Tuple

import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np
from jax.experimental import optix

from rljax.algorithm.base import QLearning
from rljax.network import DiscreteQuantileFunction
from rljax.util import get_quantile_at_action, load_params, quantile_loss, save_params


class QRDQN(QLearning):
    name = "QR-DQN"

    def __init__(
        self,
        num_agent_steps,
        state_space,
        action_space,
        seed,
        gamma=0.99,
        nstep=1,
        buffer_size=10 ** 6,
        use_per=False,
        batch_size=32,
        start_steps=50000,
        update_interval=4,
        update_interval_target=8000,
        eps=0.01,
        eps_eval=0.001,
        eps_decay_steps=250000,
        lr=5e-5,
        units=(512,),
        num_quantiles=200,
        loss_type="huber",
        dueling_net=False,
        double_q=False,
    ):
        assert loss_type in ["l2", "huber"]
        super(QRDQN, self).__init__(
            num_agent_steps=num_agent_steps,
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            gamma=gamma,
            nstep=nstep,
            buffer_size=buffer_size,
            batch_size=batch_size,
            use_per=use_per,
            start_steps=start_steps,
            update_interval=update_interval,
            update_interval_target=update_interval_target,
            eps=eps,
            eps_eval=eps_eval,
            eps_decay_steps=eps_decay_steps,
        )

        def quantile_fn(s):
            return DiscreteQuantileFunction(
                action_space=action_space,
                num_critics=1,
                num_quantiles=num_quantiles,
                hidden_units=units,
                dueling_net=dueling_net,
            )(s)

        # Quantile network.
        self.quantile_net = hk.without_apply_rng(hk.transform(quantile_fn))
        self.params = self.params_target = self.quantile_net.init(next(self.rng), self.fake_state)
        opt_init, self.opt = optix.adam(lr, eps=0.01 / batch_size)
        self.opt_state = opt_init(self.params)

        # Fixed cumulative probabilities for calculating quantile values.
        cum_p = jnp.arange(0, num_quantiles + 1, dtype=jnp.float32) / num_quantiles
        self.cum_p_prime = jnp.expand_dims((cum_p[1:] + cum_p[:-1]) / 2.0, 0)

        # Other parameters.
        self.num_quantiles = num_quantiles
        self.loss_type = loss_type
        self.double_q = double_q

    @partial(jax.jit, static_argnums=0)
    def _forward(
        self,
        params: hk.Params,
        state: np.ndarray,
    ) -> jnp.ndarray:
        q_s = self.quantile_net.apply(params, state).mean(axis=1)
        return jnp.argmax(q_s, axis=1)

    def update(self, writer=None):
        self.learning_step += 1
        weight, batch = self.buffer.sample(self.batch_size)
        state, action, reward, done, next_state = batch

        self.opt_state, self.params, loss, abs_td = self._update(
            opt_state=self.opt_state,
            params=self.params,
            params_target=self.params_target,
            state=state,
            action=action,
            reward=reward,
            done=done,
            next_state=next_state,
            weight=weight,
        )

        # Update priority.
        if self.use_per:
            self.buffer.update_priority(abs_td)

        # Update target network.
        if self.agent_step % self.update_interval_target == 0:
            self.params_target = self._update_target(self.params_target, self.params)

        if writer and self.learning_step % 1000 == 0:
            writer.add_scalar("loss/quantile", loss, self.learning_step)

    @partial(jax.jit, static_argnums=0)
    def _update(
        self,
        opt_state: Any,
        params: hk.Params,
        params_target: hk.Params,
        state: np.ndarray,
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        next_state: np.ndarray,
        weight: np.ndarray,
    ) -> Tuple[Any, hk.Params, jnp.ndarray, jnp.ndarray]:
        (loss, abs_td), grad = jax.value_and_grad(self._loss, has_aux=True)(
            params,
            params_target=params_target,
            state=state,
            action=action,
            reward=reward,
            done=done,
            next_state=next_state,
            weight=weight,
        )
        update, opt_state = self.opt(grad, opt_state)
        params = optix.apply_updates(params, update)
        return opt_state, params, loss, abs_td

    @partial(jax.jit, static_argnums=0)
    def _loss(
        self,
        params: hk.Params,
        params_target: hk.Params,
        state: np.ndarray,
        action: np.ndarray,
        reward: np.ndarray,
        done: np.ndarray,
        next_state: np.ndarray,
        weight: np.ndarray,
    ) -> Tuple[jnp.ndarray, jnp.ndarray]:
        if self.double_q:
            # Calculate greedy actions with online network.
            next_action = self._forward(params, next_state)[..., None]
            # Then calculate max quantile values with target network.
            next_quantile = get_quantile_at_action(self.quantile_net.apply(params_target, next_state), next_action)
        else:
            # Calculate greedy actions and max quantile values with target network.
            next_quantile = jnp.max(self.quantile_net.apply(params_target, next_state), axis=2, keepdims=True)

        # Calculate target quantile values and reshape to (batch_size, 1, N).
        target_quantile = jnp.expand_dims(reward, 2) + (1.0 - jnp.expand_dims(done, 2)) * self.discount * next_quantile
        target_quantile = jax.lax.stop_gradient(target_quantile).reshape(-1, 1, self.num_quantiles)
        # Calculate current quantile values, whose shape is (batch_size, N, 1).
        curr_quantile = get_quantile_at_action(self.quantile_net.apply(params, state), action)
        td = target_quantile - curr_quantile
        loss = quantile_loss(td, self.cum_p_prime, weight, self.loss_type)
        abs_td = jnp.abs(td).sum(axis=1).mean(axis=1, keepdims=True)
        return loss, jax.lax.stop_gradient(abs_td)

    def save_params(self, save_dir):
        save_params(self.params, os.path.join(save_dir, "params.npz"))

    def load_params(self, save_dir):
        self.params = self.params_target = load_params(os.path.join(save_dir, "params.npz"))
