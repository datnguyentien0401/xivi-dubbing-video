/**
 * Wraps Firebase Auth state + Google Sign-In — used in Login.tsx and
 * ProtectedRoute (App.tsx). Do not call firebase/auth directly scattered
 * across other components (see frontend AGENTS.md, same reasoning behind
 * centralizing fetch in lib/api.ts).
 */
import { useEffect, useState } from "react";
import {
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithPopup,
  signOut,
  type User,
} from "firebase/auth";
import { auth } from "@/lib/firebase";

const googleProvider = new GoogleAuthProvider();

export function useAuth() {
  const [user, setUser] = useState<User | null>(auth.currentUser);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    return onAuthStateChanged(auth, (nextUser) => {
      setUser(nextUser);
      setLoading(false);
    });
  }, []);

  async function signInWithGoogle() {
    await signInWithPopup(auth, googleProvider);
  }

  async function signOutUser() {
    await signOut(auth);
  }

  return { user, loading, signInWithGoogle, signOutUser };
}
