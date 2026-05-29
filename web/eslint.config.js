import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import prettier from "eslint-config-prettier";
import globals from "globals";

export default tseslint.config(
  // Don't lint build output.
  { ignores: ["dist"] },

  // Base + TypeScript recommended (non-type-checked: fast, no type info required).
  js.configs.recommended,
  ...tseslint.configs.recommended,

  // React Fast Refresh (Vite) component-export hygiene.
  reactRefresh.configs.vite,

  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: {
      "react-hooks": reactHooks,
    },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
      },
    },
    rules: {
      // The two classic, high-value React Hooks rules. We intentionally do NOT
      // enable eslint-plugin-react-hooks v7's full React Compiler ruleset
      // (purity / immutability / static-components / etc.) for this first
      // conservative landing — those would flood an existing codebase.
      "react-hooks/rules-of-hooks": "error",
      // Keep exhaustive-deps as a warning for the first landing so it surfaces
      // issues without blocking CI.
      "react-hooks/exhaustive-deps": "warn",
      // Dev-time Fast Refresh hint only (no runtime impact). Colocating a hook
      // with its provider (useToast/useAuth) is intentional, so keep it a warning
      // rather than a blocking error — matches the official Vite template.
      "react-refresh/only-export-components": "warn",
    },
  },

  // Must come LAST: turns off stylistic rules that conflict with Prettier.
  prettier,
);
