import { createClient } from '@supabase/supabase-js';
import ws from 'ws';
const SUPABASE_URL = process.env.SUPABASE_URL || 'https://felyggqjjhltwokdfhop.supabase.co';
const SUPABASE_KEY = process.env.SUPABASE_KEY || 'REDACTED';
let _client = null;
export function getClient() {
    if (!_client)
        _client = createClient(SUPABASE_URL, SUPABASE_KEY, {
            auth: { persistSession: false },
            realtime: { transport: ws },
        });
    return _client;
}
