from transformers import FlaxAutoModelForSeq2SeqLM, FlaxT5EncoderModel, AutoTokenizer
import jax.numpy as jnp
import jax
import qax

from .quant import quantize_matrix, QuantMatrix

jit_quantize = jax.jit(quantize_matrix, static_argnames=("use_approx", "group_size"))


def maybe_quantize(path, param):
    if param.ndim != 2:
        return param

    if "embed" in jax.tree_util.keystr(path):
        # Don't want to relative_attention_bias embedding
        return param

    # Avoid embedding tables/final projection
    if any(d > 5000 for d in param.shape):
        return param
    return quantize_matrix(param, use_approx=True, group_size=32)


def quantize_params_tree(params):
    return jax.tree_util.tree_map_with_path(maybe_quantize, params)

if __name__ == "__main__":
    # with jax.default_device(jax.devices("cpu")[0]):
    #     # model_name = "google/t5-v1_1-xxl"
    #     # model = FlaxT5EncoderModel.from_pretrained(model_name, from_pt=True)
    #     # model.params = jax.tree.map(lambda x: x.astype(jnp.bfloat16), model.params)
    #     # model.save_pretrained("t5-v1_1-xxl-flax", params=model.params, push_to_hub=True)
    #     tokenizer = AutoTokenizer.from_pretrained("google/t5-v1_1-xxl")
    #     tokenizer.save_pretrained("t5-v1_1-xxl-flax", push_to_hub=True)
    # exit()

    model_name = "nev/t5-v1_1-xxl-flax"
    model, params = FlaxT5EncoderModel.from_pretrained(model_name, _do_init=False, dtype=jnp.bfloat16)
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    quantized_params = quantize_params_tree(params)
    params = jax.device_put_replicated(params, jax.devices("tpu"))
    wrapped_model = jax.jit(qax.use_implicit_args(model.__call__))


    def set_use_kernel(tree, value):
        def op(x):
            if isinstance(x, QuantMatrix):
                x.use_kernel = value

        jax.tree.map(op, tree, is_leaf=lambda x: isinstance(x, QuantMatrix))


    input = "The color of the sky is: "
    encoder_input = jnp.asarray(tokenizer.encode(input), dtype=jnp.int32)[None]
    decoder_start = jnp.asarray([[tokenizer.pad_token_id]], dtype=jnp.int32)

    encoder_input = encoder_input
    decoder_start = decoder_start

    # kwargs = {"input_ids": encoder_input, "attention_mask": encoder_mask, "decoder_input_ids": decoder_start, "decoder_attention_mask": decoder_mask}
    # kwargs = {"input_ids": encoder_input, "decoder_input_ids": decoder_start}
    kwargs = {"input_ids": encoder_input}
    set_use_kernel(quantized_params, True)
    quantized_logits = wrapped_model(params=quantized_params, **kwargs)
    set_use_kernel(quantized_params, False)
    quantized_logits_no_kernel = wrapped_model(params=quantized_params, **kwargs)
    # base_logits                = wrapped_model(params=params, **kwargs).logits
    exit()

    print(f"Input: {input}")
    # for (name, logits) in ("Original", base_logits), ("Quantized", quantized_logits), ("Quantized No Kernel", quantized_logits_no_kernel):
    for name, logits in (
        ("Quantized", quantized_logits),
        ("Quantized No Kernel", quantized_logits_no_kernel),
    ):
        last_logits = logits[0][-1]
        assert last_logits.ndim == 1
        probs = jax.nn.softmax(last_logits)
        top_probs, top_tokens = jax.lax.top_k(probs, 10)
        print(f"{name} predictions")
        for token, prob in zip(top_tokens, top_probs):
            print(f"\t{tokenizer.convert_ids_to_tokens(int(token))}: {prob:.2%}")