/**
 * ArkTS external scanner.
 *
 * Single external token: AUTOMATIC_SEMICOLON_INSERTION (spec §2.11:
 * "Declarations and statements are usually terminated by a line separator").
 *
 * The token is zero-width. It is valid when, after skipping whitespace and
 * comments, the scanner is at:
 *   - a line separator (a separator anywhere in the skipped trivia counts,
 *     including separators inside block comments),
 *   - a closing '}' (end of block), or
 *   - end of file.
 *
 * Line separators are spec §2.4: LF (U+000A), CR (U+000D), LS (U+2028),
 * PS (U+2029). Any sequence of them is a single separator, so the
 * saw_newline flag is idempotent (a CRLF pair just sets it twice). The
 * grammar's extras regex [\s\u00a0\ufeff\u2028\u2029] covers the full
 * §2.3/§2.4 whitespace set, so the parser itself can skip all of them
 * between tokens; this token stays zero-width and only carries the
 * "a line separator was crossed" signal.
 */
#include "tree_sitter/parser.h"

#include <stdbool.h>
#include <stdint.h>

enum TokenType {
  AUTOMATIC_SEMICOLON_INSERTION,
};

void *tree_sitter_arkts_external_scanner_create(void) { return NULL; }

void tree_sitter_arkts_external_scanner_destroy(void *payload) {
  (void)payload;
}

unsigned tree_sitter_arkts_external_scanner_serialize(
    void *payload,
    char *buffer
) {
  (void)payload;
  (void)buffer;
  return 0;
}

void tree_sitter_arkts_external_scanner_deserialize(
    void *payload,
    const char *buffer,
    unsigned length
) {
  (void)payload;
  (void)buffer;
  (void)length;
}

static void skip_one(TSLexer *lexer) {
  lexer->advance(lexer, true);
}
static bool is_digit(int32_t c) {
  return c >= '0' && c <= '9';
}

// Spec §2.3 White Spaces: U+0020, U+0009, U+000B, U+000C, U+00A0 (NBSP),
// U+FEFF (ZWNBSP). All are plain trivia with no ASI meaning; skipping
// NBSP/ZWNBSP here keeps them from breaking the trivia loop before a
// following line separator is seen (e.g. `foo()\u00a0\nbar()`).
static bool is_blank(int32_t c) {
  return c == ' ' || c == '\t' || c == '\v' || c == '\f' ||
         c == 0xa0 || c == 0xfeff;
}

// Spec §2.4 Line Separators: LF, CR, LS (U+2028), PS (U+2029).
static bool is_line_separator(int32_t c) {
  return c == '\n' || c == '\r' || c == 0x2028 || c == 0x2029;
}

bool tree_sitter_arkts_external_scanner_scan(
    void *payload,
    TSLexer *lexer,
    const bool *valid_symbols
) {
  (void)payload;

  if (!valid_symbols[AUTOMATIC_SEMICOLON_INSERTION]) {
    return false;
  }

  lexer->result_symbol = AUTOMATIC_SEMICOLON_INSERTION;
  lexer->mark_end(lexer);  // zero-width token

  if (lexer->eof(lexer)) {
    return true;
  }

  bool saw_newline = false;

  // skip whitespace and comments; a line separator anywhere makes ASI valid
  for (;;) {
    int32_t c = lexer->lookahead;

    if (c == 0) {
      break;
    }
    if (is_blank(c)) {
      lexer->advance(lexer, true);
      continue;
    }
    if (is_line_separator(c)) {
      saw_newline = true;
      lexer->advance(lexer, true);
      continue;
    }
    if (c == '/') {
      lexer->advance(lexer, true);
      if (lexer->lookahead == '/') {
        // line comment: up to (not including) the line separator
        while (lexer->lookahead != 0 &&
               !is_line_separator(lexer->lookahead)) {
          lexer->advance(lexer, true);
        }
        continue;
      }
      if (lexer->lookahead == '*') {
        // block comment: line separators inside count
        lexer->advance(lexer, true);
        bool closed = false;
        while (lexer->lookahead != 0) {
          if (is_line_separator(lexer->lookahead)) {
            saw_newline = true;
          }
          if (lexer->lookahead == '*') {
            lexer->advance(lexer, true);
            if (lexer->lookahead == '/') {
              lexer->advance(lexer, true);
              closed = true;
              break;
            }
            continue;
          }
          lexer->advance(lexer, true);
        }
        if (!closed) {
          return false;  // unterminated comment: not our token
        }
        continue;
      }
      // '/' is division or a regex start, not a terminator context
      return false;
    }
    break;
  }

  if (!saw_newline) {
    if (lexer->eof(lexer)) {
      return true;
    }
    return lexer->lookahead == '}';
  }

  // A line separator was crossed. ASI does NOT apply when the next line
  // starts with a continuation token — the statement continues (method
  // chains `.m()`, calls `(`, indexing `[`, binary operators, templates).
  // Without this, GLR forks between chain-continuation and
  // statement-end, and error recovery wins with a MISSING identifier.
  // (Same approach as tree-sitter-javascript's scanner.)
  if (lexer->eof(lexer)) {
    return true;
  }
  switch (lexer->lookahead) {
    case '`': case ',': case ':': case ';': case '*': case '%':
    case '>': case '<': case '=': case '[': case '(': case '?':
    case '^': case '|': case '&': case '/':
      return false;  // statement continues
    case '.':
      // `.m()` continues the chain; `.5` is a decimal literal — insert
      skip_one(lexer);
      return is_digit(lexer->lookahead);
    case '+':
      skip_one(lexer);
      return lexer->lookahead == '+';  // `++` inserts, binary `+` continues
    case '-':
      skip_one(lexer);
      return lexer->lookahead == '-';
    default:
      return true;
  }
}
