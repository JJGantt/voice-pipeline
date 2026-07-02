import { type MacroNutritionInput } from './helpers.js';
export interface NutritionIngredient {
    name: string;
    food?: string;
    amount?: number | null;
    unit?: string;
    calories: number | null;
    protein_g: number | null;
    carbs_g: number | null;
    fat_g: number | null;
}
export interface NutritionEntry {
    id: string;
    date: string;
    occurred_at: string;
    name: string;
    quantity: number;
    calories: number | null;
    protein_g: number | null;
    carbs_g: number | null;
    fat_g: number | null;
    ingredients: NutritionIngredient[] | null;
    logged: string;
    staged?: boolean;
    warnings?: string[];
}
export declare function logNutrition(items: {
    name?: string;
    recipe?: string;
    servings?: number;
    nutrition?: MacroNutritionInput;
    occurred_at?: string;
    ingredients?: NutritionIngredient[] | null;
}[], opts?: {
    staged?: boolean;
}): Promise<NutritionEntry[]>;
export declare function getNutrition(date?: string): Promise<NutritionEntry[]>;
export declare function stageNutrition(items: Parameters<typeof logNutrition>[0]): Promise<NutritionEntry[]>;
export declare function getStaged(): Promise<NutritionEntry[]>;
export declare function commitStaged(idPrefix: string): Promise<NutritionEntry | null>;
export declare function editStaged(idPrefix: string, fields: {
    name?: string;
    nutrition?: MacroNutritionInput;
    ingredients?: NutritionIngredient[] | null;
}): Promise<NutritionEntry | null>;
export declare function removeStaged(idPrefix: string): Promise<NutritionEntry | null>;
export declare function formatStagedList(entries: NutritionEntry[]): string;
export declare function deleteNutritionEntry(idPrefix: string): Promise<NutritionEntry | null>;
export declare function formatNutritionDay(date: string, entries: NutritionEntry[]): string;
