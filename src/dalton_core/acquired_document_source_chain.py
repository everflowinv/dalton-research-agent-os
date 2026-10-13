"""Read exact acquired originals after discovery aliases or mission carry.

These functions confer no dispatch/transport permission. The extraction owner
must first authorize the current review and source; the normal source validator
still proves every original byte and receipt after this lookup.
"""
import json

from .research_verification import ResearchVerificationConflict
from .public_web_core_fetch import url_authority_from_discovery

MAX_MISSION_ANCESTORS = 64


def acquired_annual_parent(writer, review, row, parent_ref):
    """Same-ticket parent, current scope or a strongly verified ancestor."""
    version = review['mission_version_ref']
    def lookup(ref):
        return writer.store.connection.execute(
            'SELECT * FROM coverage_mission_discovered_documents WHERE document_ref=? AND ticket_ref=? '
            'AND company_ref=? AND mission_version_ref=? AND source_ref=? AND status=\'acquired\'',
            (parent_ref,row['ticket_ref'],review['company_ref'],ref,review['source_ref'])).fetchone()
    current = lookup(version)
    if current is not None:
        return current  # original same-scope contract, authorization checked by caller
    seen = set()
    original_mission_ref = None
    for _ in range(MAX_MISSION_ANCESTORS):
        if version in seen:
            raise ResearchVerificationConflict('annual attachment mission ancestry repeats')
        seen.add(version)
        mission = writer.coverage_mission._mission_record(version)
        if original_mission_ref is None:
            original_mission_ref = mission['mission_ref']
        if mission['mission_ref'] != original_mission_ref:
            raise ResearchVerificationConflict('annual attachment mission ancestry changes owner')
        parent = lookup(version)
        if parent is not None:
            return parent
        version = mission['prior_version_ref']
        if version is None:
            break
    else:
        raise ResearchVerificationConflict('annual attachment mission ancestry exceeds read bound')
    raise ResearchVerificationConflict('annual report attachment has no acquired parent in this review ancestry')


def completed_discovery_alias_manifest(writer, launcher, review, row, spool):
    """Resolve a filing's own exact discovery URL, never guess or fetch one."""
    discovery = writer.coverage_mission.discovery_record(row['discovery_ref'])
    if (discovery['company_ref'] != review['company_ref']
            or discovery['source_ref'] != review['source_ref']):
        raise ResearchVerificationConflict('acquired document discovery source/company differs')
    envelope_row = writer.store.connection.execute(
        'SELECT record_json,content_hash FROM connector_source_envelopes WHERE source_envelope_id=?',
        (discovery['source_envelope_ref'],)).fetchone()
    if (envelope_row is None or envelope_row['content_hash'] != discovery['source_envelope_hash']
            or json.loads(envelope_row['record_json']).get('content_hash') != discovery['source_envelope_hash']):
        raise ResearchVerificationConflict('acquired document discovery envelope hash differs')
    authority = url_authority_from_discovery(writer.store.connection, spool,
        url_ref=review['document_ref'],source_envelope_ref=discovery['source_envelope_ref'])
    # The URL has its own hash-bound source identity. Both the lookup and the
    # ticket reader must agree on it before any bytes reach the review.
    binding = launcher.locate_completed_manifest_binding(authority['url_ref'])
    manifest = binding['manifest']
    if (manifest['url_ref'] != authority['url_ref']
            or manifest['canonical_url'] != authority['canonical_url']
            or manifest['host'] != authority['host']):
        raise ResearchVerificationConflict('completed alias fetch does not bind the discovered URL')
    return manifest
