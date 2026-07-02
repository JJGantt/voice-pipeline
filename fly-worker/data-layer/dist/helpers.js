import { randomUUID } from 'node:crypto';
export function now() {
    return toEastern(new Date());
}
/** Convert a Date to Eastern time string (yyyy-MM-ddTHH:mm:ss). */
export function toEastern(d) {
    return d.toLocaleString('sv-SE', { timeZone: 'America/New_York', hour12: false }).replace(' ', 'T');
}
/**
 * Ensure a date string is in Eastern time.
 * If it looks like an ISO string with Z or +00:00, parse as UTC and convert.
 * If it has no timezone indicator, parse as UTC and convert (since Claude tends to give UTC).
 * For date-only strings (yyyy-MM-dd), return as-is.
 */
export function ensureEastern(dateStr) {
    // Date-only: nothing to convert
    if (/^\d{4}-\d{2}-\d{2}$/.test(dateStr))
        return dateStr;
    // Parse and convert — Date() treats strings without Z as local, so append Z
    // to force UTC interpretation, since Claude typically sends UTC
    const hasTimezone = /[Zz]|[+-]\d{2}:\d{2}$/.test(dateStr);
    const d = new Date(hasTimezone ? dateStr : dateStr + 'Z');
    if (isNaN(d.getTime()))
        return dateStr;
    return toEastern(d);
}
export function uuid() {
    return randomUUID();
}
export function today() {
    return now().slice(0, 10);
}
/** Like ensureEastern but returns only the date portion (yyyy-MM-dd). */
export function ensureEasternDate(dateStr) {
    return ensureEastern(dateStr).slice(0, 10);
}
export class ValidationError extends Error {
    constructor(message) {
        super(message);
        this.name = 'ValidationError';
    }
}
function roundTenth(value) {
    return Math.round(value * 10) / 10;
}
function isFiniteNumber(value) {
    return typeof value === 'number' && Number.isFinite(value);
}
function formatFieldList(fields) {
    if (fields.length === 1)
        return fields[0];
    if (fields.length === 2)
        return `${fields[0]} and ${fields[1]}`;
    return `${fields.slice(0, -1).join(', ')}, and ${fields[fields.length - 1]}`;
}
export function deriveCaloriesFromMacros(protein_g, carbs_g, fat_g) {
    return Math.round((protein_g * 4) + (carbs_g * 4) + (fat_g * 9));
}
export function normalizeMacroNutrition(nutrition, context) {
    const macroFields = ['protein_g', 'carbs_g', 'fat_g'];
    const missing = macroFields.filter((field) => !isFiniteNumber(nutrition?.[field]));
    if (missing.length) {
        throw new ValidationError(`${context} requires numeric ${formatFieldList([...missing])}.`);
    }
    const negative = macroFields.filter((field) => nutrition?.[field] < 0);
    if (negative.length) {
        throw new ValidationError(`${context} requires non-negative ${formatFieldList([...negative])}.`);
    }
    const protein_g = roundTenth(nutrition.protein_g);
    const carbs_g = roundTenth(nutrition.carbs_g);
    const fat_g = roundTenth(nutrition.fat_g);
    let calories;
    if (nutrition?.calories == null) {
        calories = deriveCaloriesFromMacros(protein_g, carbs_g, fat_g);
    }
    else if (!isFiniteNumber(nutrition.calories)) {
        throw new ValidationError(`${context} requires numeric calories when calories are provided.`);
    }
    else if (nutrition.calories < 0) {
        throw new ValidationError(`${context} requires non-negative calories.`);
    }
    else {
        calories = roundTenth(nutrition.calories);
    }
    return { calories, protein_g, carbs_g, fat_g };
}
export function fmtWeight(w) {
    if (w === 0)
        return 'BW';
    return `${w === Math.floor(w) ? Math.floor(w) : w} lbs`;
}
