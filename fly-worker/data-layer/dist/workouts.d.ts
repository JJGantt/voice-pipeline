export declare const VALID_CATEGORIES: readonly ["push", "pull", "legs", "core", "cardio"];
export declare const VALID_MUSCLES: string[];
/** Validate a fractional muscle map: { slug: contribution } with known slugs and 0 < c <= 1. */
export declare function validateMuscles(muscles: unknown): Record<string, number>;
/** Validate + normalize a category set: a non-empty subset of VALID_CATEGORIES. Throws on bad input. */
export declare function validateCategories(cats: unknown): string[];
/** Human label for a derived category set: "Full body", "Push / Legs", "Pull / Legs + Core". */
export declare function categoryLabel(cats: string[]): string;
export interface CatalogExercise {
    name: string;
    categories: string[];
    muscles: Record<string, number>;
}
export declare function getCatalog(): Promise<{
    categories: string[];
    muscles: string[];
    exercises: CatalogExercise[];
}>;
/** Create or replace a catalog entry (categories + fractional muscle map). Fixes mistakes too. */
export declare function upsertCatalogExercise(name: string, categories: unknown, muscles: unknown): Promise<CatalogExercise>;
export interface SetRow {
    id: number;
    occurred_at: string;
    exercise: string;
    weight_lbs: number;
    reps: number;
}
/**
 * Log one or more sets. Each set is its own timestamped row. Unknown exercises are rejected
 * unless they carry categories + a muscles map, in which case they're registered in the catalog.
 * Sets in a single call share the call's timestamp (logged together); log set-by-set for real rest gaps.
 */
export declare function logSets(items: {
    exercise: string;
    weight_lbs: number;
    reps: number;
    categories?: string[];
    muscles?: Record<string, number>;
}[]): Promise<SetRow[]>;
export declare function removeSet(id: number): Promise<SetRow | null>;
export declare function updateSet(id: number, weight_lbs: number, reps: number): Promise<SetRow | null>;
/** All sets for a given local day (defaults today), ordered by time. */
export declare function getDaySets(date?: string): Promise<{
    date: string;
    sets: SetRow[];
}>;
/** Recent workout days (grouped from sets), newest first. */
export declare function listWorkoutDays(limit?: number): Promise<{
    date: string;
    setCount: number;
    categories: string[];
    exercises: string[];
}[]>;
export declare function getExerciseHistory(name: string, limit?: number): Promise<{
    date: string;
    sets: {
        weight_lbs: number;
        reps: number;
    }[];
}[]>;
export interface MuscleVolume {
    muscle: string;
    label: string;
    group: string;
    sets: number;
}
/** Trailing-window fractional set volume per muscle, summed across all logged sets. */
export declare function getWeeklyVolume(days?: number): Promise<{
    days: number;
    volumes: MuscleVolume[];
}>;
export declare function formatWeeklyVolume(result: {
    days: number;
    volumes: MuscleVolume[];
}): string;
/** A day's sets as a grouped table (by exercise, in order first logged). */
export declare function formatDay(date?: string): Promise<string>;
