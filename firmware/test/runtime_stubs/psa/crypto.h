#ifndef IH_TEST_PSA_H
#define IH_TEST_PSA_H
#include "runtime.h"
/* Deterministic hash API double, NOT a cryptographic implementation. Host tests
 * use real hashlib SHA-256; the IDF build links the actual PSA SHA-256 backend. */
typedef struct { uint8_t sum; bool active; } psa_hash_operation_t;
#define PSA_SUCCESS 0
#define PSA_ALG_SHA_256 1
static inline psa_hash_operation_t psa_hash_operation_init(void) { return (psa_hash_operation_t){0}; }
int psa_crypto_init(void);
int psa_hash_setup(psa_hash_operation_t *op, int algorithm);
int psa_hash_update(psa_hash_operation_t *op, const uint8_t *data, size_t size);
int psa_hash_finish(psa_hash_operation_t *op, uint8_t *hash, size_t size, size_t *length);
int psa_hash_abort(psa_hash_operation_t *op);
#endif
