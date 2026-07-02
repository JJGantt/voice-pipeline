export declare function now(): string;
/** Convert a Date to Eastern time string (yyyy-MM-ddTHH:mm:ss). */
export declare function toEastern(d: Date): string;
/**
 * Ensure a date string is in Eastern time.
 * If it looks like an ISO string with Z or +00:00, parse as UTC and convert.
 * If it has no timezone indicator, parse as UTC and convert (since Claude tends to give UTC).
 * For date-only strings (yyyy-MM-dd), return as-is.
 */
export declare function ensureEastern(dateStr: string): string;
export declare function uuid(): string;
export declare function today(): string;
/** Like ensureEastern but returns only the date portion (yyyy-MM-dd). */
export declare function ensureEasternDate(dateStr: string): string;
export declare class ValidationError extends Error {
    constructor(message: string);
}
export interface MacroNutritionInput {
    calories?: number | null;
    protein_g?: number | null;
    carbs_g?: number | null;
    fat_g?: number | null;
}
export interface MacroNutrition {
    calories: number;
    protein_g: number;
    carbs_g: number;
    fat_g: number;
}
export declare function deriveCaloriesFromMacros(protein_g: number, carbs_g: number, fat_g: number): number;
export declare function normalizeMacroNutrition(nutrition: MacroNutritionInput | null | undefined, context: string): MacroNutrition;
export declare function fmtWeight(w: number): string;
