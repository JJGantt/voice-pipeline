// Quick harness: prints the 5 most-recent sessions list_sessions would return.
// Run: node src/test-list.js
import { listRecentSessions } from './sessions.js';

const sessions = await listRecentSessions(5);
console.log(JSON.stringify(sessions, null, 2));
