#!/usr/bin/env node
/**
 * Test script for deploy argument parsing
 * Tests the new target-based syntax and backward compatibility
 */

const testCases = [
  // New syntax
  { args: [], expected: { target: null, environment: 'development', flag: '', mode: 'async' }},
  { args: ['local'], expected: { target: 'local', environment: 'development', flag: '-l', mode: 'async' }},
  { args: ['debug'], expected: { target: 'debug', environment: 'development', flag: '-d', mode: 'async' }},
  { args: ['dev'], expected: { target: null, environment: 'development', flag: '', mode: 'async' }},
  { args: ['prod'], expected: { target: null, environment: 'production', flag: '', mode: 'async' }},
  { args: ['local', '-b'], expected: { target: 'local', environment: 'development', flag: '-l', mode: 'blocking' }},
  { args: ['prod', '-b'], expected: { target: null, environment: 'production', flag: '', mode: 'blocking' }},

  // Legacy syntax (should still work)
  { args: ['-l'], expected: { target: 'local', environment: 'development', flag: '-l', mode: 'async' }},
  { args: ['-d'], expected: { target: 'debug', environment: 'development', flag: '-d', mode: 'async' }},
  { args: ['development', '-l'], expected: { target: 'local', environment: 'development', flag: '-l', mode: 'async' }},
  { args: ['production', '-l'], expected: { target: 'local', environment: 'production', flag: '-l', mode: 'async' }},
];

function parseArgs(argv) {
  let environment = 'development';
  let target = null;
  let flag = '';
  let mode = 'async';
  let firstPositionalArgProcessed = false;

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];

    if (arg === '--blocking' || arg === '-b') {
      mode = 'blocking';
    } else if (arg === '-l') {
      console.warn('⚠️  Warning: -l is deprecated, use "deploy local" instead');
      target = 'local';
      flag = arg;
    } else if (arg === '-d') {
      console.warn('⚠️  Warning: -d is deprecated, use "deploy debug" instead');
      target = 'debug';
      flag = arg;
    } else if ((arg === '-p' || arg === '--skip-build') ||
               (arg === '-y' || arg === '--skip-push')) {
      flag = arg.startsWith('--') ? arg.substring(2, 3) : arg.substring(1);
      if (flag === 's') flag = 'p';
    } else if (!firstPositionalArgProcessed && !arg.startsWith('-')) {
      firstPositionalArgProcessed = true;

      if (arg === 'local') {
        target = 'local';
        flag = '-l';
      } else if (arg === 'debug') {
        target = 'debug';
        flag = '-d';
      } else if (arg === 'dev' || arg === 'development') {
        environment = 'development';
      } else if (arg === 'prod' || arg === 'production') {
        environment = 'production';
      } else {
        environment = arg;
      }
    }
  }

  if (!flag && target) {
    flag = target === 'local' ? '-l' : '-d';
  }

  return { target, environment, flag, mode };
}

console.log('Testing deploy argument parsing...\n');

let passed = 0;
let failed = 0;

for (const testCase of testCases) {
  const result = parseArgs(testCase.args);
  const matches =
    result.target === testCase.expected.target &&
    result.environment === testCase.expected.environment &&
    result.flag === testCase.expected.flag &&
    result.mode === testCase.expected.mode;

  if (matches) {
    console.log(`✅ PASS: ${JSON.stringify(testCase.args)}`);
    passed++;
  } else {
    console.log(`❌ FAIL: ${JSON.stringify(testCase.args)}`);
    console.log(`   Expected: ${JSON.stringify(testCase.expected)}`);
    console.log(`   Got:      ${JSON.stringify(result)}`);
    failed++;
  }
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
