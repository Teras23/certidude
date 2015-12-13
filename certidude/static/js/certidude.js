$(document).ready(function() {
    console.info("Loading CA, to debug: curl " + window.location.href + " --negotiate -u : -H 'Accept: application/json'");
    $.ajax({
        method: "GET",
        url: "/api/",
        dataType: "json",
        error: function(response) {
            if (response.responseJSON) {
                var msg = response.responseJSON
            } else {
                var msg = { title: "Error " + response.status, description: response.statusText }
            }
            $("#container").html(nunjucks.render('error.html', { message: msg }));
        },
        success: function(session, status, xhr) {
            console.info("Got:", session);

            console.info("Opening EventSource from:", session.event_channel);

            var source = new EventSource(session.event_channel);

            source.onmessage = function(event) {
                console.log("Received server-sent event:", event);
            }

            source.addEventListener("log-entry", function(e) {
                var entry = JSON.parse(e.data);
                console.info("Received log entry:", entry, "gonna prepend:", $("#log_level_" + entry.severity).prop("checked"));
                if ($("#log_level_" + entry.severity).prop("checked")) {
                    $("#log_entries").prepend(nunjucks.render("logentry.html", {
                        entry: {
                            created: new Date(entry.created).toLocaleString(),
                            message: entry.message,
                            severity: entry.severity
                        }
                    }));
                }
            });

            source.addEventListener("up-client", function(e) {
                console.log("Adding security association:" + e.data);
                var lease = JSON.parse(e.data);
                var $status = $("#signed_certificates [data-dn='" + lease.identity + "'] .status");
                $status.html(nunjucks.render('status.html', {
                    lease: {
                        address: lease.address,
                        identity: lease.identity,
                        acquired: new Date(),
                        released: null
                    }}));
            });

            source.addEventListener("down-client", function(e) {
                console.log("Removing security association:" + e.data);
                var lease = JSON.parse(e.data);
                var $status = $("#signed_certificates [data-dn='" + lease.identity + "'] .status");
                $status.html(nunjucks.render('status.html', {
                    lease: {
                        address: lease.address,
                        identity: lease.identity,
                        acquired: null,
                        released: new Date()
                    }}));
            });

            source.addEventListener("request_deleted", function(e) {
                console.log("Removing deleted request #" + e.data);
                $("#request_" + e.data).remove();
            });

            source.addEventListener("request_submitted", function(e) {
                console.log("Request submitted:", e.data);
                $.ajax({
                    method: "GET",
                    url: "/api/request/" + e.data + "/",
                    dataType: "json",
                    success: function(request, status, xhr) {
                        console.info(request);
                        $("#pending_requests").prepend(
                            nunjucks.render('request.html', { request: request }));
                    }
                });

            });

            source.addEventListener("request_signed", function(e) {
                console.log("Request signed:", e.data);
                $("#request_" + e.data).slideUp("normal", function() { $(this).remove(); });

                $.ajax({
                    method: "GET",
                    url: "/api/signed/" + e.data + "/",
                    dataType: "json",
                    success: function(certificate, status, xhr) {
                        console.info(certificate);
                        $("#signed_certificates").prepend(
                            nunjucks.render('signed.html', { certificate: certificate }));
                    }
                });
            });

            source.addEventListener("certificate_revoked", function(e) {
                console.log("Removing revoked certificate #" + e.data);
                $("#certificate_" + e.data).slideUp("normal", function() { $(this).remove(); });
            });

            $("#container").html(nunjucks.render('authority.html', { session: session, window: window }));


            $.ajax({
                method: "GET",
                url: "/api/log/",
                dataType: "json",
                success:function(entries, status, xhr) {
                    console.info("Got", entries.length, "log entries");
                    for (var j = 0; j < entries.length; j++) {
                        if ($("#log_level_" + entries[j].severity).prop("checked")) {
                            $("#log_entries").append(nunjucks.render("logentry.html", {
                                entry: {
                                    created: new Date(entries[j].created).toLocaleString("et-EE"),
                                    message: entries[j].message,
                                    severity: entries[j].severity
                                }
                            }));
                        }
                    }
                }
            });

            $.ajax({
                method: "GET",
                url: "/api/lease/",
                dataType: "json",
                success: function(leases, status, xhr) {
                    console.info("Got leases:", leases);
                    for (var j = 0; j < leases.length; j++) {
                        var $status = $("#signed_certificates [data-dn='" + leases[j].identity + "'] .status");
                        if (!$status.length) {
                            console.info("Detected rogue client:", leases[j]);
                            continue;
                        }
                        $status.html(nunjucks.render('status.html', {
                            lease: {
                                address: leases[j].address,
                                identity: leases[j].identity,
                                acquired: new Date(leases[j].acquired).toLocaleString(),
                                released: leases[j].released ? new Date(leases[j].released).toLocaleString() : null
                            }}));
                    }

                    /* Set up search box */
                    $("#search").on("keyup", function() {
                        var q = $("#search").val().toLowerCase();
                        $(".filterable").each(function(i, e) {
                            if ($(e).attr("data-dn").toLowerCase().indexOf(q) >= 0) {
                                $(e).show();
                            } else {
                                $(e).hide();
                            }
                        });
                    });
                }
            });
        }
    });
});