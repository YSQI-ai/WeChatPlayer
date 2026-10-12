// The deployed API endpoint lives in config.local.js, which is git-ignored, so
// the real host is not published with the repository.  Copy config.example.js
// to config.local.js and put your own endpoint there.
//
// The fallback below keeps the project runnable (and the flow tests passing)
// for anyone who has not created the local file yet.
let API_BASE_URL = 'https://api.example.com';

try {
  API_BASE_URL = require('./config.local.js').API_BASE_URL || API_BASE_URL;
} catch (error) {
  // No local override: fall back to the placeholder above.
}

module.exports = { API_BASE_URL };
