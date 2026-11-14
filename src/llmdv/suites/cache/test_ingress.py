"""One prefix read through every ingress in front of the same engines.

Clients of one fleet reach it through more than one ingress, such as a gateway for some tools
and a direct route for others. Where the profile declares that ingresses share one prefix
cache, a prompt computed through one must be read through another. A layer that changes the
prompt it forwards, or keys the cache by its caller, recomputes every prefix the other computed.
"""

from __future__ import annotations

import pytest

from ...evidence import journal
from ...plugin import register_client
from .helpers import cache_config, check_cold, check_replicas, check_warm
from .sessions import observe_on
from .test_render import single_block

pytestmark = [pytest.mark.surface_generic("cache_usage"), pytest.mark.needs("cache_reuse")]


@pytest.mark.case_id("cache.cross_ingress")
def test_cross_ingress_reuse(request, client, client_factory, profile, surface, model, ingress):
    """A prompt computed through another ingress to the same engines is read here."""
    config = cache_config(ingress, surface)
    peers = [peer for peer in config.shared_with if peer != ingress.id]
    if ingress.id not in config.shared_with or not peers:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no shared_with peer")
    journal.record("shared_cache_ingresses", peers)
    targets = [profile.ingress(peer) for peer in peers]
    for target in targets:
        peer_config = target.capabilities.cache_reuse
        if peer_config is None or surface.name not in peer_config.surfaces:
            pytest.skip(
                f"[undeclared] ingress {target.id!r} declares no cache reuse on {surface.name}"
            )
        if surface.name not in target.surfaces:
            pytest.skip(f"[undeclared] ingress {target.id!r} does not expose {surface.name}")
    for target in targets:
        conversation = single_block(config.prefix_words)
        peer_client = client_factory(target.id)
        # The factory attached the peer's client to this case; the verdict is about this one.
        register_client(request.node, ingress, client)
        first = observe_on(peer_client, surface, target.model, target, conversation, target.id)
        here = observe_on(client, surface, model, ingress, conversation, ingress.id)
        check_replicas([first, here], config)
        check_cold(first, config)
        check_warm(first, here, config)
