export interface WeightEntry {
    id: number;
    date: string;
    weight_lbs: number;
    logged_at: string;
}
export interface NutritionTargetEntry {
    id: number;
    date: string;
    calories: number;
    protein_g: number;
    carbs_g: number;
    fat_g: number;
    logged_at: string;
}
export declare function getProfile(): Promise<{
    height_in: number | null;
    updated: string | null;
}>;
export declare function setHeight(heightIn: number): Promise<void>;
export declare function logWeight(weightLbs: number, date?: string): Promise<WeightEntry>;
export declare function recentWeights(limit?: number): Promise<WeightEntry[]>;
export declare function setNutritionTargets(targets: {
    calories: number;
    protein_g: number;
    carbs_g: number;
    fat_g: number;
}, date?: string): Promise<NutritionTargetEntry>;
export declare function recentTargets(limit?: number): Promise<NutritionTargetEntry[]>;
export declare function formatProfile(profile: {
    height_in: number | null;
}, weights: WeightEntry[], targets?: NutritionTargetEntry[]): string;
