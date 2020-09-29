import math
from typing import Any, Tuple

import haiku as hk
import jax
import jax.numpy as jnp


@jax.jit
def calculate_gaussian_log_prob(
    log_std: jnp.ndarray,
    noise: jnp.ndarray,
) -> jnp.ndarray:
    """
    Calculate log probabilities of diagonal gaussian distributions.
    """
    return (-0.5 * jnp.square(noise) - log_std).sum(axis=1, keepdims=True) - 0.5 * jnp.log(2 * math.pi) * log_std.shape[1]


@jax.jit
def calculate_log_pi(
    log_std: jnp.ndarray,
    noise: jnp.ndarray,
    action: jnp.ndarray,
) -> jnp.ndarray:
    """
    Calculate log probabilities of the policies, which is diagonal gaussian distributions followed by tanh transformation.
    """
    return calculate_gaussian_log_prob(log_std, noise) - jnp.log(1 - jnp.square(action) + 1e-6).sum(axis=1, keepdims=True)


@jax.jit
def evaluate_lop_pi(
    mean: jnp.ndarray,
    log_std: jnp.ndarray,
    action: jnp.ndarray,
) -> jnp.ndarray:
    """
    Calculate log probabilities of the policies given sampled actions.
    """
    noise = (jnp.arctanh(action) - mean) / (jnp.exp(log_std) + 1e-8)
    return calculate_log_pi(log_std, noise, action)


@jax.jit
def reparameterize(
    mean: jnp.ndarray,
    log_std: jnp.ndarray,
    key: jnp.ndarray,
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    """
    Calculate stochastic actions and log probabilities.
    """
    std = jnp.exp(log_std)
    noise = jax.random.normal(key, std.shape)
    action = jnp.tanh(mean + noise * std)
    return action, calculate_log_pi(log_std, noise, action)


@jax.jit
def clip_gradient(
    grad: Any,
    max_grad_norm: float,
) -> Any:
    """
    Clip gradients.
    """
    return jax.tree_multimap(lambda g: jnp.clip(g, -max_grad_norm, max_grad_norm), grad)


@jax.jit
def soft_update(
    target_params: hk.Params,
    online_params: hk.Params,
    tau: float,
) -> hk.Params:
    """
    Update target network using Polyak-Ruppert Averaging.
    """
    return jax.tree_multimap(lambda t, s: (1 - tau) * t + tau * s, target_params, online_params)


@jax.jit
def add_noise(
    x: jnp.ndarray,
    key: jnp.ndarray,
    std: float,
    x_min: float,
    x_max: float,
) -> jnp.ndarray:
    """
    Add noise to actions.
    """
    return jnp.clip(x + jax.random.normal(key, x.shape) * std, x_min, x_max)


@jax.jit
def get_q_at_action(
    q_s: jnp.ndarray,
    action: jnp.ndarray,
) -> jnp.ndarray:
    def _get(q_s, action):
        return q_s[action]

    return jax.vmap(_get)(q_s, action)


@jax.jit
def get_quantile_at_action(
    quantile_s: jnp.ndarray,
    action: jnp.ndarray,
) -> jnp.ndarray:
    def _get(quantile_s, action):
        return quantile_s[:, action]

    return jax.vmap(_get)(quantile_s, action)


@jax.jit
def _huber_loss(
    td: jnp.ndarray,
    kappa: float = 1.0,
) -> jnp.ndarray:
    abs_td = jnp.abs(td)
    return jnp.where(abs_td <= kappa, 0.5 * jnp.square(td), kappa * (abs_td - 0.5 * kappa))


@jax.jit
def calculate_quantile_huber_loss(
    td: jnp.ndarray,
    tau: jnp.ndarray,
    weight: jnp.ndarray,
    kappa: float = 1.0,
) -> jnp.ndarray:
    element_wise_loss = _huber_loss(td, kappa)
    element_wise_loss *= jnp.abs(tau[..., None] - (jax.lax.stop_gradient(td) < 0)) / kappa
    batch_loss = element_wise_loss.sum(axis=1).mean(axis=1, keepdims=True)
    return (batch_loss * weight).mean()
