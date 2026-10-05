export const ALLOWED_PROBABILITY_STATUSES = new Set([
  "calibrated_shadow", "insufficient_data", "insufficient_evidence", "not_generated",
]);

export const PENDING_PROBABILITY_STAGES = new Set([
  "source_capture_pending", "source_index_verification_pending", "maintenance_pending",
]);

export function unavailableAuthorityStage(stage) {
  return PENDING_PROBABILITY_STAGES.has(stage) || stage === "maintenance_failed";
}

export function requireUnquarantinedProbability(availability, status, filterQualified, error) {
  if (availability === "outcome_evidence_quarantined"
      && (status === "calibrated_shadow" || filterQualified === true)) {
    throw error("历史证据已隔离，不得保留概率或筛选授权");
  }
}
