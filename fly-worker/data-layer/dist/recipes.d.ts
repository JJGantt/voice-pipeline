import { type MacroNutrition, type MacroNutritionInput } from './helpers.js';
export interface Ingredient {
    item: string;
    quantity?: number;
    unit?: string;
    notes?: string;
}
export interface Recipe {
    id: string;
    name: string;
    notes: string;
    tags: string[];
    ingredients: Ingredient[];
    steps: string[];
    nutrition: {
        calories?: number;
        protein_g?: number;
        carbs_g?: number;
        fat_g?: number;
    } | null;
    created: string;
    updated: string;
}
export declare function listRecipes(tag?: string): Promise<Recipe[]>;
export declare function getRecipe(name: string): Promise<Recipe | null>;
export declare function saveRecipe(args: {
    name: string;
    ingredients: Ingredient[];
    steps: string[];
    notes?: string;
    tags?: string[];
    nutrition: MacroNutritionInput;
}): Promise<{
    recipe: Recipe;
    action: string;
}>;
export declare function deleteRecipe(name: string): Promise<Recipe | null>;
export declare function getRecipeNutrition(name: string): Promise<MacroNutrition | null>;
export declare function formatRecipe(r: Recipe): string;
