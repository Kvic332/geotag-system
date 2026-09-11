package com.geotag.demo

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.geotag.sdk.ErrorListener
import com.geotag.sdk.GeoTagClient
import com.geotag.sdk.GeofenceEventCallback

class MainActivity : AppCompatActivity() {

    private var geoTagClient: GeoTagClient? = null

    private lateinit var tokenInput: EditText
    private lateinit var statusText: TextView
    private lateinit var startButton: Button
    private lateinit var stopButton: Button

    private val locationPermission = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        if (grants[Manifest.permission.ACCESS_FINE_LOCATION] == true ||
            grants[Manifest.permission.ACCESS_COARSE_LOCATION] == true
        ) {
            startTracking()
        } else {
            statusText.text = "Location permission denied — grant it in Settings"
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        tokenInput = findViewById(R.id.tokenInput)
        statusText = findViewById(R.id.statusText)
        startButton = findViewById(R.id.startButton)
        stopButton = findViewById(R.id.stopButton)

        stopButton.isEnabled = false

        startButton.setOnClickListener { onStartClicked() }
        stopButton.setOnClickListener { onStopClicked() }
    }

    private fun onStartClicked() {
        val token = tokenInput.text.toString().trim()
        if (token.isEmpty()) {
            Toast.makeText(this, "Paste your JWT token first", Toast.LENGTH_SHORT).show()
            return
        }

        val hasLocation =
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) ==
                PackageManager.PERMISSION_GRANTED

        if (!hasLocation) {
            statusText.text = "Requesting location permission…"
            locationPermission.launch(
                arrayOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION
                )
            )
        } else {
            startTracking()
        }
    }

    private fun startTracking() {
        val token = tokenInput.text.toString().trim()

        geoTagClient?.shutdown()
        geoTagClient = GeoTagClient.Builder(this)
            .apiUrl(BuildConfig.API_URL)
            .apiKey(token)
            .batchIntervalSeconds(10)
            .locationIntervalSeconds(10)
            .build()

        geoTagClient?.setEventCallback(object : GeofenceEventCallback {
            override fun onEnter(deviceId: String, geofenceId: String) {
                Toast.makeText(this@MainActivity, "Entered zone $geofenceId", Toast.LENGTH_LONG).show()
            }

            override fun onExit(deviceId: String, geofenceId: String) {
                Toast.makeText(this@MainActivity, "Exited zone $geofenceId", Toast.LENGTH_LONG).show()
            }

            override fun onDwell(deviceId: String, geofenceId: String, dwellMs: Long) {
                val secs = dwellMs / 1000
                Toast.makeText(this@MainActivity, "Dwell in $geofenceId after ${secs}s", Toast.LENGTH_LONG).show()
            }
        })

        geoTagClient?.setErrorListener(ErrorListener { error ->
            statusText.text = "SDK error: ${error.message}"
        })

        geoTagClient?.startTracking()

        statusText.text = "Tracking active — sending GPS every 10s to Railway"
        startButton.isEnabled = false
        stopButton.isEnabled = true
        tokenInput.isEnabled = false
    }

    private fun onStopClicked() {
        geoTagClient?.stopTracking()
        statusText.text = "Tracking stopped"
        startButton.isEnabled = true
        stopButton.isEnabled = false
        tokenInput.isEnabled = true
    }

    override fun onDestroy() {
        super.onDestroy()
        geoTagClient?.shutdown()
    }
}
