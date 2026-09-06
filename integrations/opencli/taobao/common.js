// Vendored from @jackwener/opencli 1.8.7 (Apache-2.0); see ../LICENSE.opencli.
// Modified by opencli-admin: preserve delivered commerce fixes and local helper closure.
/**
 * Shared utilities for CLI adapters.
 */
import { ArgumentError } from '@jackwener/opencli/errors';
/**
 * Clamp a numeric value to [min, max].
 * Matches the signature of lodash.clamp and Rust's clamp.
 */
export function clamp(value, min, max) {
    return Math.max(min, Math.min(value, max));
}
export function clampInt(raw, fallback, min, max) {
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) {
        return fallback;
    }
    return clamp(Math.floor(parsed), min, max);
}
export function requireNonEmptyQuery(value, label = 'query') {
    const normalized = String(value ?? '').trim();
    if (!normalized) {
        throw new ArgumentError(`${label} cannot be empty`);
    }
    return normalized;
}
