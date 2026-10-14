import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merges conditional class names (clsx) then dedupes conflicting Tailwind
 * utilities (tailwind-merge) — e.g. cn("px-2", condition && "px-4") keeps
 * only "px-4" instead of leaving both in the className string. Standard
 * shadcn/ui helper, used by every component in components/ui/. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
