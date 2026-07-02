export interface ListItem {
    id: string;
    list_name: string;
    text: string;
    done: boolean;
    deleted: boolean;
    created: string;
    updated: string;
}
export declare function availableLists(): Promise<string[]>;
export declare function listItems(listName: string): Promise<ListItem[]>;
export declare function addItem(listName: string, text: string): Promise<ListItem>;
export declare function checkItem(listName: string, idPrefix: string): Promise<ListItem | null>;
export declare function uncheckItem(listName: string, idPrefix: string): Promise<ListItem | null>;
export declare function removeItem(listName: string, idPrefix: string): Promise<ListItem | null>;
export declare function createList(listName: string): Promise<boolean>;
export declare function formatListItems(items: ListItem[], listName: string): string;
