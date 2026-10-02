import e18e from '@e18e/eslint-plugin'
import json from '@eslint/json'
import tsParser from '@typescript-eslint/parser'
import { defineConfig } from 'eslint/config'
import vueParser from 'vue-eslint-parser'

export default defineConfig([
  { ignores: ['dist/**'] },
  {
    files: ['**/*.{js,ts,vue}'],
    extends: [e18e.configs.recommended],
  },
  {
    files: ['**/*.{ts,vue}'],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
        extraFileExtensions: ['.vue'],
      },
    },
  },
  {
    files: ['**/*.vue'],
    languageOptions: {
      parser: vueParser,
      parserOptions: { parser: tsParser },
    },
  },
  {
    files: ['package.json'],
    plugins: { json },
    language: 'json/json',
    extends: [e18e.configs.moduleReplacements],
  },
])
