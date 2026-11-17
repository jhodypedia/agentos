import next from "eslint-config-next";

const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "public/**"] },
  ...next,
  {
    files: ["**/*.ts", "**/*.tsx"],
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      // Data-fetching hooks and "sync form with loaded record" patterns set
      // state from an effect on purpose; the React Compiler rule would forbid
      // every fetch-on-mount hook.
      "react-hooks/set-state-in-effect": "off",
    },
  },
];

export default config;
