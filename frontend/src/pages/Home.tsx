import { Link } from "react-router-dom";
import {
  Activity,
  AudioLines,
  BookOpen,
  Captions,
  CheckCircle2,
  Globe2,
  Languages,
  Mic2,
  ShieldCheck,
  Sparkles,
  Users,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

const NAV_LINKS = [
  { href: "#features", label: "Features" },
  { href: "#how-it-works", label: "How it works" },
  { href: "#languages", label: "Languages" },
];

const FEATURES = [
  {
    icon: AudioLines,
    title: "Consistent character voices",
    description:
      "Every character keeps the same voice across an entire series, instead of a single generic narrator reading every line.",
    card: "bg-[hsl(243_70%_97%)] border-[hsl(243_45%_89%)]",
    iconBox: "bg-[hsl(243_65%_92%)] text-[hsl(243_60%_48%)]",
  },
  {
    icon: Sparkles,
    title: "Transcreation, not translation",
    description:
      "Songs and rhymes are re-written to keep rhythm and meaning in the target language, not translated word-for-word.",
    card: "bg-[hsl(201_75%_97%)] border-[hsl(201_50%_88%)]",
    iconBox: "bg-[hsl(201_70%_90%)] text-[hsl(201_70%_36%)]",
  },
  {
    icon: ShieldCheck,
    title: "Cultural safety per market",
    description:
      "Every localized version goes through a cultural safety review tuned to each target country before it reaches a human reviewer.",
    card: "bg-[hsl(336_70%_97%)] border-[hsl(336_48%_90%)]",
    iconBox: "bg-[hsl(336_68%_92%)] text-[hsl(336_55%_48%)]",
  },
  {
    icon: Activity,
    title: "Real-time pipeline status",
    description:
      "Every job streams live status — transcribing, translating, dubbing — so you're never left guessing how far along it is.",
    card: "bg-[hsl(32_80%_96%)] border-[hsl(32_52%_87%)]",
    iconBox: "bg-[hsl(32_75%_89%)] text-[hsl(26_65%_40%)]",
  },
  {
    icon: Users,
    title: "Human review, always",
    description:
      "No content is auto-published. A reviewer approves every dub before it ships — especially important for kids' content.",
    card: "bg-[hsl(152_50%_96%)] border-[hsl(152_36%_87%)]",
    iconBox: "bg-[hsl(152_48%_89%)] text-[hsl(152_48%_30%)]",
  },
  {
    icon: BookOpen,
    title: "Learning material, generated for free",
    description:
      "Vocabulary flashcards and review questions are generated alongside every episode, for parents and teachers.",
    card: "bg-[hsl(271_60%_97%)] border-[hsl(271_40%_90%)]",
    iconBox: "bg-[hsl(271_55%_92%)] text-[hsl(271_48%_48%)]",
  },
];

const HOW_IT_WORKS = [
  {
    icon: Captions,
    title: "Transcribe & diarize",
    description:
      "Gemini transcribes the original audio and tells speakers apart, line by line, with timestamps.",
  },
  {
    icon: Mic2,
    title: "Profile character voices",
    description:
      "Each speaker is matched to a consistent character voice, cross-referenced against earlier episodes in the series.",
  },
  {
    icon: Languages,
    title: "Translate & transcreate",
    description:
      "Dialogue is translated; songs and rhymes go through a dedicated transcreation pass that preserves rhythm instead of literal meaning.",
  },
  {
    icon: ShieldCheck,
    title: "Cultural safety check",
    description:
      "Gemini flags anything that may be inappropriate for the target market, with a plain-language explanation for the reviewer.",
  },
  {
    icon: AudioLines,
    title: "Dub with Gemini Native Audio",
    description:
      "Each line is voiced in the character's own voice, timed to fit the original performance.",
  },
  {
    icon: CheckCircle2,
    title: "Human review & publish",
    description:
      "A reviewer checks every flagged line and the final cut before anything goes live — no agent publishes on its own.",
  },
];

const LANGUAGES = [
  "Vietnamese",
  "Indonesian",
  "Thai",
  "Tagalog",
  "Japanese",
  "Korean",
];

function Logo({ className = "" }: { className?: string }) {
  return (
    <span className={`flex items-center gap-2 ${className}`}>
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-[hsl(263_75%_59%)] font-display text-base font-bold text-primary-foreground">
        X
      </span>
      <span className="font-display text-lg font-semibold text-foreground">
        XIVI Studio
      </span>
    </span>
  );
}

/** Public landing page — the only route reachable without signing in (see
 * App.tsx). Introduces the product and funnels visitors toward /login;
 * everything else in the app lives behind ProtectedRoute. */
export default function Home() {
  return (
    <div className="min-h-screen bg-background">
      <header className="sticky top-0 z-10 border-b bg-background/80 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
          <Logo />
          <div className="flex items-center gap-8">
            <nav className="hidden items-center gap-8 md:flex">
              {NAV_LINKS.map((link) => (
                <a
                  key={link.href}
                  href={link.href}
                  className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
                >
                  {link.label}
                </a>
              ))}
            </nav>
            <div className="flex items-center gap-2">
              <Button asChild variant="ghost">
                <Link to="/login">Sign in</Link>
              </Button>
              <Button asChild className="hidden sm:inline-flex">
                <Link to="/studio">Dashboard</Link>
              </Button>
            </div>
          </div>
        </div>
      </header>

      <main>
        <section className="relative overflow-hidden">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute left-1/2 top-0 -z-10 h-[32rem] w-[64rem] -translate-x-1/2 -translate-y-1/3 rounded-full bg-primary/15 blur-3xl"
          />
          <div className="mx-auto max-w-4xl px-6 py-10 text-center sm:py-14">
            <Badge
              variant="secondary"
              className="gap-1.5 bg-accent px-3 py-1 text-accent-foreground"
            >
              <Sparkles size={14} />
              Powered by Gemini — text, vision & native audio
            </Badge>
            <h1 className="mt-6 font-display text-4xl font-semibold tracking-tight sm:text-5xl">
              Localize kids' animated videos
              <span className="text-primary"> without losing their voice</span>
            </h1>
            <p className="mx-auto mt-6 max-w-2xl text-lg text-muted-foreground">
              XIVI Studio is an agentic translation and dubbing pipeline, built
              for series that need to keep every character sounding like
              themselves — in every language.
            </p>
            <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
              <Button asChild size="lg">
                <Link to="/login">Get started</Link>
              </Button>
              <Button asChild size="lg" variant="outline">
                <a href="#how-it-works">See how it works</a>
              </Button>
            </div>
            <div
              id="languages"
              className="mt-8 flex flex-wrap items-center justify-center gap-2 text-sm text-muted-foreground"
            >
              <Globe2 size={16} className="shrink-0" />
              <span>Built for JAPAC languages, including</span>
              {LANGUAGES.map((lang) => (
                <span
                  key={lang}
                  className="rounded-full border bg-card px-2.5 py-0.5 text-foreground shadow-sm"
                >
                  {lang}
                </span>
              ))}
            </div>
          </div>
        </section>

        <section id="features" className="scroll-mt-20 px-6 py-12 sm:py-16">
          <div className="mx-auto max-w-6xl rounded-3xl border bg-card px-6 py-10 shadow-sm sm:px-10 sm:py-14">
            <div className="mx-auto max-w-2xl text-center">
              <h2 className="font-display text-3xl font-semibold">
                Standout features
              </h2>
              <p className="mt-3 text-muted-foreground">
                Built around what makes kids' content localization different
                from a typical dubbing tool.
              </p>
            </div>
            <div className="mt-10 grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
              {FEATURES.map(({ icon: Icon, title, description, card, iconBox }) => (
                <Card
                  key={title}
                  className={`${card} transition-all hover:-translate-y-0.5 hover:shadow-md`}
                >
                  <CardHeader>
                    <div
                      className={`mb-2 flex h-11 w-11 items-center justify-center rounded-lg ${iconBox}`}
                    >
                      <Icon size={22} />
                    </div>
                    <CardTitle>{title}</CardTitle>
                    <CardDescription>{description}</CardDescription>
                  </CardHeader>
                </Card>
              ))}
            </div>
          </div>
        </section>

        <section
          id="how-it-works"
          className="scroll-mt-20 border-y bg-card/60 px-6 py-16 sm:py-20"
        >
          <div className="mx-auto max-w-6xl">
            <div className="mx-auto max-w-2xl text-center">
              <h2 className="font-display text-3xl font-semibold">
                How it works
              </h2>
              <p className="mt-3 text-muted-foreground">
                One pipeline, six Gemini-powered agents, each doing exactly
                what it's strongest at.
              </p>
            </div>
            <ol className="mt-12 grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
              {HOW_IT_WORKS.map(({ icon: Icon, title, description }, index) => (
                <li key={title}>
                  <Card className="h-full">
                    <CardHeader>
                      <div className="mb-2 flex items-center gap-3">
                        <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                          <Icon size={20} />
                        </div>
                        <span className="font-display text-sm font-semibold text-muted-foreground">
                          Step {index + 1}
                        </span>
                      </div>
                      <CardTitle>{title}</CardTitle>
                      <CardDescription>{description}</CardDescription>
                    </CardHeader>
                  </Card>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section className="px-6 py-14 sm:py-20">
          <div className="mx-auto flex max-w-4xl flex-col items-center gap-6 rounded-2xl bg-primary px-6 py-14 text-center text-primary-foreground sm:py-16">
            <h2 className="font-display text-3xl font-semibold">
              Ready to localize your series?
            </h2>
            <p className="max-w-xl text-primary-foreground/80">
              Sign in with Google and bring your first episode through the
              pipeline — translation, transcreation, dubbing, and a review
              step built in.
            </p>
            <Button asChild size="lg" variant="secondary">
              <Link to="/login">Get started for free</Link>
            </Button>
          </div>
        </section>
      </main>

      <footer className="border-t px-6 py-8 text-center text-sm text-muted-foreground">
        XIVI Studio — localization for kids' animated video.
      </footer>
    </div>
  );
}
