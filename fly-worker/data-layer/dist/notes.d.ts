export interface Note {
    name: string;
    content: string;
    updated: string;
}
export declare function availableNotes(): Promise<string[]>;
export declare function getNote(name: string): Promise<Note | null>;
export declare function setNote(name: string, content: string): Promise<void>;
export declare function allNotes(): Promise<Note[]>;
