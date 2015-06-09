/*global describe, it, expect, example, before, after, beforeEach, afterEach, mocha, sinon*/
'use strict';
var assert = require('chai').assert;
var ko = require('knockout');

var koHelpers = require('../koHelpers');
// Add sinon asserts to chai.assert, so we can do assert.calledWith instead of sinon.assert.calledWith
sinon.assert.expose(assert, {prefix: ''});

describe('koHelpers', () => {
    describe('sanitizedObservable', () => {
        it('removes html', () => {
            var obs = koHelpers.sanitizedObservable();
            ['foo', '<b>foo</b>', '<b>foo'].forEach((input) => {
                obs(input);
                assert.equal(obs(), 'foo', input + ' is sanitized correctly');
            });
        });
    });

    // TODO: test custom validators

    describe('mapJStoKO', () => {
        var data = {
            thursday: 'Before Friday',
            object2: {
                offer: 'Greatly appreciated',
                other: 5
            },
            array2: [5, 4, 3, 2, 1],
            more: 'The other day',
            number: 243
        };

        it('test all observable', () => {
            var dataOut = koHelpers.mapJStoKO(data);
            assert.isTrue(ko.isObservable(dataOut.thursday));
            assert.isTrue(ko.isObservable(dataOut.object2));
            assert.isTrue(ko.isObservable(dataOut.array2));
            assert.isTrue(ko.isObservable(dataOut.more));
            assert.isTrue(ko.isObservable(dataOut.number));
        });

        it('test exclusion', () => {
            var dataOut = koHelpers.mapJStoKO(data, {exclude: ['object2', 'array2']});
            assert.isFalse(ko.isObservable(dataOut.object2));
            assert.isFalse(ko.isObservable(dataOut.array2));
        });

        it('test ko array', () => {
            var dataOut = koHelpers.mapJStoKO(data, {exclude: ['object2', 'thursday', 'more', 'number']});
            assert.isTrue(ko.isObservable(dataOut.array2));
            assert.isTrue(Array.isArray(dataOut.array2()));
        });
    });
});
