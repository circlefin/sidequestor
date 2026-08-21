/* Copyright 2026 Circle Internet Group, Inc. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fail_status(OSStatus status) {
    fprintf(stderr, "keychain operation failed (status %d)\n", (int)status);
    return 1;
}

static unsigned char *read_all_stdin(size_t *length) {
    size_t capacity = 4096;
    unsigned char *buffer = malloc(capacity);
    if (buffer == NULL) return NULL;
    *length = 0;
    while (!feof(stdin)) {
        if (*length == capacity) {
            capacity *= 2;
            unsigned char *larger = realloc(buffer, capacity);
            if (larger == NULL) {
                free(buffer);
                return NULL;
            }
            buffer = larger;
        }
        *length += fread(buffer + *length, 1, capacity - *length, stdin);
        if (ferror(stdin)) {
            free(buffer);
            return NULL;
        }
    }
    return buffer;
}

int main(int argc, char **argv) {
    if (argc != 4 || (strcmp(argv[1], "read") != 0 && strcmp(argv[1], "write") != 0)) {
        fprintf(stderr, "usage: keychain-helper <read|write> <service> <account>\n");
        return 3;
    }

    const char *service = argv[2];
    const char *account = argv[3];
    SecKeychainItemRef item = NULL;

    if (strcmp(argv[1], "read") == 0) {
        UInt32 length = 0;
        void *data = NULL;
        OSStatus status = SecKeychainFindGenericPassword(
            NULL, (UInt32)strlen(service), service, (UInt32)strlen(account), account,
            &length, &data, &item);
        if (status == errSecItemNotFound) return 44;
        if (status != errSecSuccess) return fail_status(status);
        if (length > 0 && fwrite(data, 1, length, stdout) != length) {
            SecKeychainItemFreeContent(NULL, data);
            if (item != NULL) CFRelease(item);
            return 2;
        }
        SecKeychainItemFreeContent(NULL, data);
        if (item != NULL) CFRelease(item);
        return 0;
    }

    size_t input_length = 0;
    unsigned char *input = read_all_stdin(&input_length);
    if (input == NULL || input_length > UINT32_MAX) {
        free(input);
        return 2;
    }

    OSStatus status = SecKeychainFindGenericPassword(
        NULL, (UInt32)strlen(service), service, (UInt32)strlen(account), account,
        NULL, NULL, &item);
    if (status == errSecItemNotFound) {
        status = SecKeychainAddGenericPassword(
            NULL, (UInt32)strlen(service), service, (UInt32)strlen(account), account,
            (UInt32)input_length, input, &item);
    } else if (status == errSecSuccess) {
        status = SecKeychainItemModifyAttributesAndData(
            item, NULL, (UInt32)input_length, input);
    }
    free(input);
    if (item != NULL) CFRelease(item);
    return status == errSecSuccess ? 0 : fail_status(status);
}
