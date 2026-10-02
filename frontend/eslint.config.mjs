import next from 'eslint-config-next'

/**
 * Flat ESLint config. `eslint-config-next` v16 exports an array of flat
 * configs covering core-web-vitals, TypeScript and React hooks rules.
 */
const config = [
  ...next,
  {
    ignores: [
      'node_modules/**',
      '.next/**',
      'out/**',
      'build/**',
      'next-env.d.ts',
      'coverage/**',
    ],
  },
]

export default config
