/**
 * Firebase init — used for Auth (sign-in) and Firestore (realtime job
 * status). Fill in the real config from the Firebase Console before running
 * (do not commit config with a real API key if the repo is public — use
 * VITE_* environment variables).
 */
import { getAuth } from "firebase/auth";
import { initializeApp } from "firebase/app";
import { getFirestore } from "firebase/firestore";

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
};

const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = getFirestore(app);
