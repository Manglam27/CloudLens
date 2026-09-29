import 'dart:convert';

import 'package:amplify_auth_cognito/amplify_auth_cognito.dart';
import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:http/http.dart' as http;

/// Client for the credibility check API (Check API Lambda on cloudlens-api).
///
/// Every request carries the signed-in user's Cognito ID token; API Gateway's
/// JWT authorizer rejects anything else before it reaches the Lambda.
class CheckService {
  static const String baseUrl = 'https://ieip1diyzc.execute-api.us-east-1.amazonaws.com';

  static Future<Map<String, String>> _headers() async {
    final session = await Amplify.Auth.fetchAuthSession() as CognitoAuthSession;
    final idToken = session.userPoolTokensResult.value.idToken.raw;
    return {'Authorization': 'Bearer $idToken', 'Content-Type': 'application/json'};
  }

  static Map<String, dynamic> _decode(http.Response response, List<int> okStatuses) {
    Map<String, dynamic> body;
    try {
      body = jsonDecode(response.body) as Map<String, dynamic>;
    } catch (_) {
      body = {};
    }
    if (!okStatuses.contains(response.statusCode)) {
      throw CheckException(body['error'] as String? ?? 'Request failed (${response.statusCode}).');
    }
    return body;
  }

  /// Starts a check for one of the user's cloud photos (FR 1.4).
  static Future<CheckResult> start(String imageKey) async {
    final response = await http.post(
      Uri.parse('$baseUrl/checks'),
      headers: await _headers(),
      body: jsonEncode({'imageKey': imageKey}),
    );
    // 202: queued for the worker. 200: this screenshot was checked before.
    return CheckResult.fromJson(_decode(response, [200, 202]));
  }

  /// Reads the current state of a check, used for polling and results (FR 1.4, 1.5).
  static Future<CheckResult> get(String checkId) async {
    final response = await http.get(Uri.parse('$baseUrl/checks/$checkId'), headers: await _headers());
    return CheckResult.fromJson(_decode(response, [200]));
  }

  /// The user's past checks, newest first (FR 1.6).
  static Future<List<CheckResult>> history() async {
    final response = await http.get(Uri.parse('$baseUrl/checks'), headers: await _headers());
    final checks = _decode(response, [200])['checks'] as List<dynamic>? ?? [];
    return checks.map((c) => CheckResult.fromJson(c as Map<String, dynamic>)).toList();
  }
}

class CheckException implements Exception {
  final String message;

  CheckException(this.message);

  @override
  String toString() => message;
}

class CheckResult {
  final String checkId;
  final String status;
  final String? imageKey;
  final String? verdict;
  final int? score;
  final String? reason;
  final String? claim;
  final String? error;
  final DateTime? createdAt;
  final bool cached;

  CheckResult({
    required this.checkId,
    required this.status,
    this.imageKey,
    this.verdict,
    this.score,
    this.reason,
    this.claim,
    this.error,
    this.createdAt,
    this.cached = false,
  });

  bool get isDone => status == 'DONE';

  bool get isFailed => status == 'FAILED';

  bool get isFinished => isDone || isFailed;

  factory CheckResult.fromJson(Map<String, dynamic> json) => CheckResult(
        checkId: json['checkId'] as String,
        status: json['status'] as String,
        imageKey: json['imageKey'] as String?,
        verdict: json['verdict'] as String?,
        score: (json['score'] as num?)?.toInt(),
        reason: json['reason'] as String?,
        claim: json['claim'] as String?,
        error: json['error'] as String?,
        createdAt: DateTime.tryParse(json['createdAt'] as String? ?? '')?.toLocal(),
        cached: json['cached'] as bool? ?? false,
      );
}
