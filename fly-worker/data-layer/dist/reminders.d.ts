export interface Reminder {
    id: string;
    text: string;
    due: string | null;
    done: boolean;
    deleted: boolean;
    created: string;
    updated: string;
}
export declare function listReminders(): Promise<Reminder[]>;
export declare function addReminder(text: string, due?: string): Promise<Reminder>;
export declare function completeReminder(idPrefix: string): Promise<Reminder | null>;
export declare function removeReminder(idPrefix: string): Promise<Reminder | null>;
export declare function formatReminders(items: Reminder[]): string;
