import nextCoreWebVitals from 'eslint-config-next/core-web-vitals';
import nextTypescript from 'eslint-config-next/typescript';
import prettier from 'eslint-config-prettier/flat';

const eslintConfig = [
    ...nextCoreWebVitals,
    ...nextTypescript,
    prettier,
    {
        rules: {
            '@typescript-eslint/no-unused-vars': 'off', // 不检查未使用的变量
            '@typescript-eslint/no-explicit-any': 'off', // 关闭 any 报错
        },
    },
];

export default eslintConfig;
