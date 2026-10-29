/*
 * Custom build only: Settings > Updates (custom build).
 */
package dev.patrickgold.florisboard.app.custom

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.patrickgold.florisboard.BuildConfig
import dev.patrickgold.florisboard.R
import dev.patrickgold.florisboard.lib.compose.FlorisScreen
import dev.patrickgold.jetpref.datastore.model.collectAsState as collectPrefAsState
import dev.patrickgold.jetpref.datastore.ui.PreferenceGroup
import dev.patrickgold.jetpref.datastore.ui.SwitchPreference
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.florisboard.lib.compose.stringRes
import java.io.File

/** Screen state for the update check; kept in a holder so the home-screen banner can reuse it. */
sealed class UpdateCheckState {
    object Idle : UpdateCheckState()
    object Checking : UpdateCheckState()
    object UpToDate : UpdateCheckState()
    data class Available(val release: CustomRelease) : UpdateCheckState()
    data class Failed(val message: String) : UpdateCheckState()
}

/** Last result of a check, shared between the home-screen card and this screen. */
object CustomUpdateState {
    var check: UpdateCheckState by mutableStateOf(UpdateCheckState.Idle)
        private set

    suspend fun runCheck(markTime: suspend (Long) -> Unit) {
        check = UpdateCheckState.Checking
        check = try {
            val latest = CustomUpdater.fetchLatest()
            markTime(System.currentTimeMillis())
            when {
                latest == null -> UpdateCheckState.UpToDate
                CustomUpdater.isNewer(latest) -> UpdateCheckState.Available(latest)
                else -> UpdateCheckState.UpToDate
            }
        } catch (e: Exception) {
            UpdateCheckState.Failed(e.message ?: e.javaClass.simpleName)
        }
    }
}

@Composable
fun CustomUpdateScreen() = FlorisScreen {
    title = stringRes(R.string.custom__updates__title)
    previewFieldVisible = false

    content {
        val context = LocalContext.current
        val scope = rememberCoroutineScope()
        val lastCheck by prefs.custom.lastUpdateCheck.collectPrefAsState()
        var downloadJob by remember { mutableStateOf<Job?>(null) }
        var downloaded by remember { mutableStateOf(0L) }
        var total by remember { mutableStateOf(-1L) }
        var downloadError by remember { mutableStateOf<String?>(null) }
        var readyFile by remember { mutableStateOf<File?>(null) }

        val state = CustomUpdateState.check
        val release = (state as? UpdateCheckState.Available)?.release

        // Entering the screen always checks, unless a check is running already.
        LaunchedEffect(Unit) {
            if (state !is UpdateCheckState.Checking) {
                CustomUpdateState.runCheck { prefs.custom.lastUpdateCheck.set(it) }
            }
        }
        // Drop stale APKs; keep a finished download for the release on offer.
        LaunchedEffect(release?.tag) {
            CustomUpdater.cleanCache(context, release)
            readyFile = release?.let { CustomUpdater.downloadedFile(context, it) }
            downloadError = null
        }

        PreferenceGroup(title = stringRes(R.string.custom__updates__installed_title)) {
            Column(modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
                Text(
                    text = BuildConfig.VERSION_NAME,
                    style = MaterialTheme.typography.bodyLarge,
                    fontWeight = FontWeight.Medium,
                )
                Text(
                    text = stringRes(R.string.custom__updates__built_from, "tag" to CustomUpdater.BUILT_TAG.ifBlank { "-" }, "repo" to CustomUpdater.REPO),
                    style = MaterialTheme.typography.bodySmall,
                )
                if (lastCheck > 0L) {
                    Text(
                        text = stringRes(R.string.custom__updates__last_check, "when" to java.text.DateFormat.getDateTimeInstance().format(java.util.Date(lastCheck))),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                if (!CustomUpdater.hasToken) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        text = stringRes(R.string.custom__updates__no_token),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
        }

        PreferenceGroup(title = stringRes(R.string.custom__updates__title)) {
            SwitchPreference(
                prefs.custom.autoCheckUpdates,
                title = stringRes(R.string.custom__updates__auto_check__label),
                summary = stringRes(R.string.custom__updates__auto_check__summary),
            )
            Column(modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
                Row {
                    OutlinedButton(
                        enabled = state !is UpdateCheckState.Checking,
                        onClick = {
                            scope.launch { CustomUpdateState.runCheck { this@content.prefs.custom.lastUpdateCheck.set(it) } }
                        },
                    ) { Text(stringRes(R.string.custom__updates__check_now)) }
                }
                Spacer(Modifier.height(8.dp))
                when (state) {
                    UpdateCheckState.Idle -> Unit
                    UpdateCheckState.Checking -> Text(stringRes(R.string.custom__updates__checking))
                    UpdateCheckState.UpToDate -> Text(stringRes(R.string.custom__updates__up_to_date))
                    is UpdateCheckState.Failed -> Text(
                        text = state.message,
                        color = MaterialTheme.colorScheme.error,
                    )
                    is UpdateCheckState.Available -> Unit
                }
            }
        }

        if (release != null) {
            Card(modifier = Modifier.padding(16.dp).fillMaxWidth()) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        text = stringRes(R.string.custom__updates__available, "tag" to release.tag),
                        style = MaterialTheme.typography.titleMedium,
                    )
                    Text(
                        text = "${release.assetName} · ${CustomUpdater.formatSize(release.assetSize)}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    if (release.notes.isNotBlank()) {
                        Spacer(Modifier.height(8.dp))
                        Text(text = release.notes.trim(), style = MaterialTheme.typography.bodySmall)
                    }
                    Spacer(Modifier.height(12.dp))
                    val job = downloadJob
                    val file = readyFile
                    when {
                        job != null && job.isActive -> {
                            val progress = if (total > 0) (downloaded.toFloat() / total.toFloat()).coerceIn(0f, 1f) else null
                            if (progress != null) {
                                LinearProgressIndicator(progress = { progress }, modifier = Modifier.fillMaxWidth())
                            } else {
                                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                            }
                            Spacer(Modifier.height(8.dp))
                            Text(
                                text = "${CustomUpdater.formatSize(downloaded)} / ${CustomUpdater.formatSize(total)}",
                                style = MaterialTheme.typography.bodySmall,
                            )
                            Spacer(Modifier.height(8.dp))
                            OutlinedButton(onClick = { job.cancel() }) {
                                Text(stringRes(R.string.custom__updates__cancel))
                            }
                        }
                        file != null -> {
                            Text(
                                text = stringRes(R.string.custom__updates__ready),
                                style = MaterialTheme.typography.bodySmall,
                            )
                            Spacer(Modifier.height(8.dp))
                            Button(onClick = { CustomUpdater.install(context, file) }) {
                                Text(stringRes(R.string.custom__updates__install))
                            }
                        }
                        else -> {
                            Button(onClick = {
                                downloadError = null
                                downloaded = 0L
                                total = release.assetSize
                                downloadJob = scope.launch {
                                    try {
                                        val f = CustomUpdater.download(context, release) { d, t ->
                                            downloaded = d
                                            total = t
                                        }
                                        readyFile = f
                                        CustomUpdater.install(context, f)
                                    } catch (e: kotlinx.coroutines.CancellationException) {
                                        throw e
                                    } catch (e: Exception) {
                                        downloadError = e.message ?: e.javaClass.simpleName
                                    }
                                }
                            }) { Text(stringRes(R.string.custom__updates__download_install)) }
                        }
                    }
                    val err = downloadError
                    if (err != null) {
                        Spacer(Modifier.height(8.dp))
                        Text(text = err, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                    }
                    Spacer(Modifier.height(8.dp))
                    Row {
                        OutlinedButton(onClick = {
                            context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(release.htmlUrl)))
                        }) { Text(stringRes(R.string.custom__updates__open_release)) }
                        Spacer(Modifier.width(8.dp))
                    }
                }
            }
        }
    }
}
