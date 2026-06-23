import { createClient, SupabaseClient } from '@supabase/supabase-js';
import ws from 'ws';

const SUPABASE_URL = process.env.SUPABASE_URL || 'https://felyggqjjhltwokdfhop.supabase.co';
const SUPABASE_KEY = process.env.SUPABASE_KEY || 'REDACTED';

let _client: SupabaseClient | null = null;

export function getClient(): SupabaseClient {
  if (!_client) _client = createClient(SUPABASE_URL, SUPABASE_KEY, {
    auth: { persistSession: false },
    realtime: { transport: ws as any },
  });
  return _client;
}
