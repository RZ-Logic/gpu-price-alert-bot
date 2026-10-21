"""catalog: loading, autocomplete search() and free-text resolve(). Offline."""

import pytest

from gpu_alerts import catalog


def test_vram_options_on_every_model_without_a_fixed_vram():
    # vram_gb set: the title must state that size. Otherwise vram_options lists the sizes the
    # desktop card is sold with, and a title stating another size is a different card.
    for model in catalog.load():
        if model.vram_gb is not None:
            assert model.vram_options is None, model.id
        else:
            assert model.vram_options and isinstance(model.vram_options, tuple), model.id
        hash(model)


def test_every_model_resolves_by_its_own_id_and_name():
    for model in catalog.load():
        assert catalog.resolve(model.id) is model
        assert catalog.resolve(model.name) is model
        assert catalog.resolve(model.name.upper()) is model


@pytest.mark.parametrize("text,model_id", [
    ("RTX 5080", "rtx-5080"),
    ("rtx-5070-ti", "rtx-5070-ti"),
    ("RTX 5060 Ti 8GB", "rtx-5060-ti-8gb"),
    ("rtx 5060 ti 16 gb", "rtx-5060-ti-16gb"),
    ("RTX 5060 Ti 16G", "rtx-5060-ti-16gb"),
    ("RX 9060 XT 8GB", "rx-9060-xt-8gb"),
])
def test_resolve_free_text(text, model_id):
    model = catalog.resolve(text)
    assert model is not None and model.id == model_id, (text, model)


def test_ambiguous_text_resolves_to_nothing():
    assert catalog.resolve("5070") is None
    assert catalog.resolve("RTX 5060 Ti") is None      # 8GB or 16GB: the user picks


@pytest.mark.parametrize("text,plain_id", [
    # The plain model has no VRAM in its name, so a typed size must not narrow to the Ti or XT.
    ("RTX 5060 8GB", "rtx-5060"),
    ("rtx 5060 8 gb", "rtx-5060"),
    ("5060 8gb", "rtx-5060"),
    ("RTX 5060 16GB", "rtx-5060"),
    ("RX 9060 8GB", "rx-9060"),
    ("RTX 4060 8GB", "rtx-4060"),
    ("RTX 4060 8G", "rtx-4060"),
])
def test_vram_size_never_narrows_to_a_sibling(text, plain_id):
    model = catalog.resolve(text)
    assert model is None or model.id == plain_id, (text, model)
    assert plain_id in [m.id for m in catalog.search(text)], text


def test_search_drops_models_with_another_vram_size():
    ids = [m.id for m in catalog.search("RTX 5060 8GB")]
    assert "rtx-5060-ti-8gb" in ids and "rtx-5060-ti-16gb" not in ids
