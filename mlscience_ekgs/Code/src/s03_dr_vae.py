from functools import partial
from typing import Sequence
from tqdm import tqdm

import flax.linen as nn
from flax.training import train_state
import jax
from jax.flatten_util import ravel_pytree
from jax.tree_util import tree_map
import jax.numpy as jnp
import jax.random as jr
import optax

# def f1(x):
#     return x[0]

# def f2(x):
#     return x[0] + x[1]

# def generate_anomalies(f1, f2, x0, n_iter=1000, tol=1.0, lr=1e-2):
#     x_morphed = x0.copy()
#     for _ in range(n_iter):
#         x_delta = grad(f2)(x_morphed)
#         grad1 = grad(f1)(x_morphed)
#         # orthogonalize with respect to f1
#         x_delta = x_delta - (x_delta @ grad1) * grad1/jnp.linalg.norm(grad1)**2
#         x_morphed = x_morphed + lr * x_delta

#         if jnp.abs(f2(x_morphed) - f2(x0)) > tol:
#             break
#     if jnp.abs(f2(x_morphed) - f2(x0)) > tol:
#         print(f"x_original: {x0}")
#         print(f"x_morphed : {x_morphed}\n")
#         print(f"f1(x_original): {f1(x0)}")
#         print(f"f2(x_original): {f2(x0)}\n")
#         print(f"f1(x_morphed) : {f1(x_morphed)}")
#         print(f"f2(x_morphed) : {f2(x_morphed)}")

#         return x0, x_morphed

#     return None, None


# Encoder that returns Gaussian moments
class Encoder(nn.Module):
    features: Sequence[int]
    activation: nn.Module = nn.relu

    @nn.compact
    def __call__(self, x):
        x = x.ravel()
        for feat in self.features[:-1]:
            x = self.activation(nn.Dense(feat)(x))
        y1 = nn.Dense(self.features[-1])(x)
        y2 = nn.Dense(self.features[-1])(x)
        y2 = nn.softplus(y2)

        return y1, y2


# CNN-based encoder that returns Gaussian moments
class CNNEncoder(nn.Module):
    output_dim: int
    activation: nn.Module = nn.relu

    @nn.compact
    def __call__(self, x):
        x = jnp.transpose(x, (1, 0)) # to (batch_size, time, channel)
        x = nn.Conv(features=12, kernel_size=(10,))(x)
        x = self.activation(x)
        x = nn.avg_pool(x, window_shape=(2,), strides=(2,))
        x = nn.Conv(features=12, kernel_size=(10,))(x)
        x = self.activation(x)
        x = nn.avg_pool(x, window_shape=(2,), strides=(2,))
        x = x.ravel()
        x = nn.Dense(features=128)(x)
        x = self.activation(x)
        y1 = nn.Dense(features=self.output_dim)(x).ravel()
        y2 = nn.Dense(features=self.output_dim)(x).ravel()
        y2 = nn.softplus(y2)

        return y1, y2

# Decoder
class Decoder(nn.Module):
    features: Sequence[int]
    activation: nn.Module = nn.relu
    use_bias: bool = True

    @nn.compact
    def __call__(self, x):
        x = x.ravel()
        for feat in self.features[:-1]:
            x = self.activation(nn.Dense(feat)(x))
        x = nn.Dense(self.features[-1], 
                     use_bias=self.use_bias)(x)

        return x
    

# class CNNDecoder(nn.Module):
#     output_dim: int
#     activation: nn.Module = nn.relu
    
#     @nn.compact
#     def __call__(self, x):
#         x = x.ravel()
#         print('dec1', x.shape)
#         x = nn.Dense(features=128)(x)
#         x = self.activation(x)
#         print('dec2', x.shape)
#         x = nn.Dense(features=256)(x)
#         x = self.activation(x)
#         print('dec3', x.shape)
#         x = x.reshape((-1, 16, 16))
#         x = nn.ConvTranspose(features=12, kernel_size=(10,))(x)
#         x = self.activation(x)
#         x = nn.ConvTranspose(features=1, kernel_size=(10,))(x)
#         x = x.ravel()

#         return x
    
    
def gaussian_kl(mu, sigmasq):
    """KL divergence from a diagonal Gaussian to the standard Gaussian."""
    return -0.5 * jnp.sum(1. + jnp.log(sigmasq) - mu**2. - sigmasq)

def gaussian_sample(key, mu, sigmasq):
    """Sample a diagonal Gaussian."""
    return mu + jnp.sqrt(sigmasq) * jr.normal(key, mu.shape)

def gaussian_logpdf(x_pred, x):
    """Gaussian log pdf of data x given x_pred."""
    return -0.5 * jnp.sum((x - x_pred)**2., axis=-1)

def losses(key, params, split_idx, input, encoder_apply, decoder_apply):
    """Monte Carlo estimate of the negative evidence lower bound."""
    enc_params, dec_params = params[:split_idx], params[split_idx:]
    mu, sigmasq = encoder_apply(enc_params, input)
    z_pred = gaussian_sample(key, mu, sigmasq)
    x_pred = decoder_apply(dec_params, z_pred).reshape(input.shape)
    loss_rec = -gaussian_logpdf(x_pred, input)
    loss_kl = gaussian_kl(mu, sigmasq)

    return loss_rec, loss_kl, x_pred


def dr_reg(input, x_pred, pred_fn):
    x_disc = pred_fn(input)
    x_pred_disc = pred_fn(x_pred)
    dr_reg_val = (x_disc - x_pred_disc) ** 2

    return dr_reg_val


def binary_loss(key, params, split_idx, input, encoder_apply, decoder_apply,
                pred_fn, beta1, beta2):
    """Binary cross-entropy loss."""
    loss_rec, loss_kl, x_pred = losses(
        key, params, split_idx, input, encoder_apply, decoder_apply
    )
    dr_reg_val = dr_reg(input, x_pred, pred_fn)
    result = loss_rec + beta1 * loss_kl + beta2 * dr_reg_val

    return result, (loss_rec, loss_kl, beta2 * dr_reg_val)


@partial(jax.jit, static_argnums=(3, 4, 5, 6, 7))
def train_step(i, state, batch, encoder_apply, decoder_apply, split_idx,
               pred_fn, beta1_scheduler, beta2):
    key = jr.PRNGKey(i)
    beta1 = 1 - beta1_scheduler(i)
    binary_loss_fn = lambda params, key, input: binary_loss(
        key, params, split_idx, input, encoder_apply, decoder_apply,
        pred_fn, beta1, beta2
    )
    keys = jr.split(key, len(batch))
    loss_fn = lambda params: tree_map(
        lambda x: jnp.mean(x),
        jax.vmap(binary_loss_fn, (None, 0, 0))(params, keys, batch)
    )
    (loss, (loss_rec, loss_kl, dr_reg_val)), grads = \
        jax.value_and_grad(loss_fn, has_aux=True)(state.params)
    state = state.apply_gradients(grads=grads)  # Optimizer update step

    return state, loss, (loss_rec, loss_kl, dr_reg_val)


def train_dr_vae(pred_fn, X_train, beta1, beta2, z_dim,
                 key=0, n_epochs=100, batch_size=128, hidden_width=50,
                 hidden_depth=2, lr_init=1e-5, lr_peak=1e-4, lr_end=1e-6,
                 encoder_type="mlp", use_bias=True,
                 beta1_scheduler_type="warmup_cosine"):
    assert beta1_scheduler_type in ["constant", "linear", "cosine",
                                    "warmup_cosine", "cyclical"]
    if isinstance(key, int):
        key = jr.PRNGKey(key)

    _, *x_dim = X_train.shape
    x_dim = jnp.array(x_dim)
    n = len(X_train)

    hidden_feats = [hidden_width] * hidden_depth
    encoder_feats = [*hidden_feats, z_dim]
    decoder_feats = [*hidden_feats, jnp.prod(x_dim)]

    key_enc, key_dec = jr.split(key)

    # Encoder
    if encoder_type == "mlp":
        encoder = Encoder(encoder_feats)
    elif encoder_type == "cnn":
        encoder = CNNEncoder(z_dim)
    else:
        raise ValueError(f"Unknown encoder type: {encoder_type}")
    params_enc = encoder.init(key_enc, jnp.ones(x_dim,))['params']
    params_enc, unflatten_fn_enc = ravel_pytree(params_enc)
    print(f"Encoder params size: {params_enc.shape}")
    apply_fn_enc = lambda params, x: encoder.apply(
        {'params': unflatten_fn_enc(params)}, x
    )

    # Decoder
    decoder = Decoder(decoder_feats, use_bias=use_bias)
    params_dec = decoder.init(key_dec, jnp.ones(z_dim,))['params']
    params_dec, unflatten_fn_dec = ravel_pytree(params_dec)
    print(f"Decoder params size: {params_dec.shape}")
    apply_fn_dec = lambda params, x: decoder.apply(
        {'params': unflatten_fn_dec(params)}, x
    )
    params = jnp.array([*params_enc, *params_dec])
    split_idx = len(params_enc)

    # Train state
    n_steps = n_epochs * (n // batch_size)
    lr_schedule = optax.warmup_cosine_decay_schedule(
        init_value=lr_init,
        peak_value=lr_peak,
        warmup_steps=100,
        decay_steps=n_steps - 100,
        end_value=lr_end
    )
    optimizer = optax.adam(lr_schedule)
    state = train_state.TrainState.create(
        apply_fn=None, params=params, tx=optimizer
    )

    pbar = tqdm(range(n_epochs), desc=f"Epoch 0 average loss: 0.0")
    losses, losses_rec, losses_kl, losses_dr = [], [], [], []
    ctr = 0
    if beta1_scheduler_type == "constant":
        beta1_scheduler = lambda x: 1.0 - beta1
    elif beta1_scheduler_type == "linear":
        beta1_scheduler = optax.linear_schedule(
            init_value=1.0, end_value=1.0-beta1, transition_steps=n_steps // 2
        )
    elif beta1_scheduler_type == "cosine":
        beta1_scheduler = optax.cosine_decay_schedule(
            1.0, n_steps // 2, alpha=1.0-beta1
        )
    elif beta1_scheduler_type == "warmup_cosine":
        beta1_scheduler = optax.join_schedules(
            [optax.linear_schedule(1.0, 1.0-beta1, n_steps // 4),
             optax.cosine_decay_schedule(1.0, n_steps // 2, alpha=1.0-beta1)],
            [n_steps // 4]
        )
    elif beta1_scheduler_type == "cyclical":
        beta1_scheduler = optax.join_schedules(
            [optax.linear_schedule(1.0, 1.0-beta1, n_steps // 8)] * 4,
            [x * n_steps // 4 for x in range(1, 4)]
        )
    else:
        raise ValueError(f"Unknown beta1 scheduler type: {beta1_scheduler_type}")
    for epoch in pbar:
        key = jr.PRNGKey(epoch)
        idx = jr.permutation(key, n)
        X_train = X_train[idx]
        n_batch = n // batch_size
        if n % batch_size != 0:
            n_batch += 1
        losses_epoch = []

        for idx in range(n_batch):
            lb, ub = idx * batch_size, (idx+1) * batch_size
            X_batch = X_train[lb:ub]
            state, loss, (loss_rec, loss_kl, dr_reg_val) = train_step(
                ctr, state, X_batch, apply_fn_enc, apply_fn_dec,
                split_idx, pred_fn, beta1_scheduler, beta2
            )
            losses_epoch.append(loss)
            losses_rec.append(loss_rec)
            losses_kl.append(loss_kl)
            losses_dr.append(dr_reg_val)
            ctr += 1
        pbar.set_description(f"Epoch {epoch} average loss: "
                             f"{jnp.mean(jnp.array(losses_epoch))}")
        losses.extend(losses_epoch)
    
    # Compute statistics of encoded moments
    def _step(carry, x):
        mu, sigmasq = apply_fn_enc(state.params[:split_idx], x)
        return (mu, sigmasq), (mu, sigmasq)

    carry_init = apply_fn_enc(state.params[:split_idx], X_train[0])
    _, (mus, sigmasqs) = jax.lax.scan(_step, carry_init, X_train)
    mu_mean, mu_std = jnp.mean(mus, axis=0), jnp.std(mus, axis=0)
    sigmasq_mean, sigmasq_std = jnp.mean(sigmasqs, axis=0), \
        jnp.std(sigmasqs, axis=0)
    
    losses, losses_rec, losses_kl, losses_dr = jnp.array(losses), \
        jnp.array(losses_rec), jnp.array(losses_kl), jnp.array(losses_dr)
    
    params_enc, params_dec = state.params[:split_idx], state.params[split_idx:]
    result = {
        "params_enc": params_enc,
        "apply_fn_enc": apply_fn_enc,
        "params_dec": params_dec,
        "apply_fn_dec": apply_fn_dec,
        "mu_mean": mu_mean,
        "mu_std": mu_std,
        "sigmasq_mean": sigmasq_mean,
        "sigmasq_std": sigmasq_std,
        "losses": losses,
        "losses_rec": losses_rec,
        "losses_kl": losses_kl,
        "losses_dr": losses_dr
    }

    return result
