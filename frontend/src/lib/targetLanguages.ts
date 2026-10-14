// Shared between NewProject.tsx and NewScriptProject.tsx — kept in lib/
// (not re-exported from one page module into another) so the two page
// modules don't end up importing each other's unrelated dependencies.

export const SOURCE_LANGUAGES = [
  { code: "vi", label: "Tiếng Việt" },
  { code: "ja", label: "日本語 (Japanese)" },
  { code: "ko", label: "한국어 (Korean)" },
  { code: "zh", label: "中文 (Chinese)" },
  { code: "en", label: "English" },
];

// Target JAPAC languages — grouped by region, flag per country (see
// docs/frontend-spec.md section 3.2: "multi-select target languages,
// checkboxes with country flags, grouped by JAPAC region").
export const TARGET_LANGUAGE_GROUPS: {
  region: string;
  languages: {
    code: string;
    label: string;
    englishLabel: string;
    flag: string;
  }[];
}[] = [
  {
    region: "Southeast Asia",
    languages: [
      {
        code: "vi",
        label: "Tiếng Việt",
        englishLabel: "Vietnamese",
        flag: "🇻🇳",
      },
      {
        code: "id",
        label: "Bahasa Indonesia",
        englishLabel: "Indonesian",
        flag: "🇮🇩",
      },
      { code: "tl", label: "Filipino", englishLabel: "Filipino", flag: "🇵🇭" },
      { code: "th", label: "ภาษาไทย", englishLabel: "Thai", flag: "🇹🇭" },
      { code: "ms", label: "Bahasa Melayu", englishLabel: "Malay", flag: "🇲🇾" },
    ],
  },
  {
    region: "East Asia",
    languages: [
      { code: "ja", label: "日本語", englishLabel: "Japanese", flag: "🇯🇵" },
      { code: "ko", label: "한국어", englishLabel: "Korean", flag: "🇰🇷" },
      { code: "zh", label: "中文", englishLabel: "Chinese", flag: "🇨🇳" },
    ],
  },
];

/** Flat code -> { label, englishLabel, flag } lookup, derived from
 * TARGET_LANGUAGE_GROUPS above — for any page that needs to display a job's
 * target_language as a full name instead of its raw code (e.g.
 * ProjectDetail.tsx's job table), without re-walking the region groups
 * itself. */
export const TARGET_LANGUAGE_BY_CODE: Record<
  string,
  { label: string; englishLabel: string; flag: string }
> = Object.fromEntries(
  TARGET_LANGUAGE_GROUPS.flatMap((group) =>
    group.languages.map((lang) => [
      lang.code,
      { label: lang.label, englishLabel: lang.englishLabel, flag: lang.flag },
    ]),
  ),
);
