// Counts describe recorded physical requests, including format/schema retries.
// Missing old receipts remain unknown; a configured maximum is not a count.
export function attemptSummary(payload) {
  const count=payload.physical_attempts;
  if (!Number.isInteger(count) || count<0) return "";
  const retries=payload.retry_count;
  return Number.isInteger(retries) && retries>=0
    ? `尝试${count}次 · 重试${retries}次` : `尝试${count}次`;
}
