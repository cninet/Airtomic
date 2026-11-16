#pragma once

#include "messages.h"

bool cloudConfigured();
const char *cloudWifiSsid();
const char *cloudWifiPassword();

// Reuse the same historyId when retrying a reading after a failed upload.
// A negative lightRaw or pir marks an unavailable reading.
bool publishToFirebase(const NodeMsg &reading, const char *historyId, int lightRaw, int pir);
