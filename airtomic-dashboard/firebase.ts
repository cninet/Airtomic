// Firebase client for the dashboard.
// The web config is not a secret (it only identifies the project); access is controlled by
// firebase/database.rules.json — the signed-in user must be listed under operators/<uid>.
import { getApps, initializeApp } from 'firebase/app';
import { getAuth } from 'firebase/auth';
import { getDatabase } from 'firebase/database';

const app =
  getApps()[0] ??
  initializeApp({
    apiKey: 'AIzaSyBbp_xiCoqc7g7XkslIRC-fCTK81NOIZ2w',
    authDomain: 'airtomic-29e37.firebaseapp.com',
    databaseURL: 'https://airtomic-29e37-default-rtdb.asia-southeast1.firebasedatabase.app',
    projectId: 'airtomic-29e37',
    appId: '1:745055215184:web:527b302d4322b7c3b37b55',
  });

export const auth = getAuth(app);
export const db = getDatabase(app);
